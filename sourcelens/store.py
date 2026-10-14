"""SQLite persistence for the code index.

Schema: one `symbols` table (name/kind/qualified_name/signature + exact byte
offsets + source path), one `imports` table (file -> imported target), and a
`files` table tracking what has been indexed so re-indexing is idempotent.

The index stores byte offsets, not file content, so a symbol's exact source
is fetched from disk on demand and the index stays small.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .indexer import Import, Symbol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL,
    indexed_at REAL NOT NULL,
    mtime REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    signature TEXT NOT NULL,
    start_byte INTEGER NOT NULL,
    end_byte INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target TEXT NOT NULL,
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,
    line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_imports_target ON imports(target);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_path);
"""

#: Bounds for a single search result set (defends against full-index dumps).
_SEARCH_LIMIT_MIN = 1
_SEARCH_LIMIT_MAX = 500
_SEARCH_LIMIT_DEFAULT = 50


@dataclass
class SearchResult:
    symbol: Symbol
    id: int


def _escape_like(query: str) -> str:
    """Escape SQL LIKE metacharacters so a query can never act as a match-all.

    Backslash is doubled first, then the `%` and `_` wildcards are escaped with
    the backslash escape character (matching the `ESCAPE '\\'` clause used in
    the query). A literal `%`/`_`/`\\` in the caller's query then matches only
    that literal character instead of broadening the pattern.
    """
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_limit(limit) -> int:
    """Coerce `limit` to a bounded int; non-integral/absent values fall back."""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = _SEARCH_LIMIT_DEFAULT
    return max(_SEARCH_LIMIT_MIN, min(limit, _SEARCH_LIMIT_MAX))


class Store:
    def __init__(self, db_path: str):
        # Tool handlers run on worker threads (the MCP SDK wraps sync tools in
        # anyio.to_thread.run_sync), so the shared connection must be usable
        # from any thread and every access serialized with a reentrant lock.
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- indexing ----

    def replace_file(self, file_path: str, language: str, mtime: float, symbols: List[Symbol], imports: List[Import]) -> None:
        """Atomically replace a file's symbols + imports (idempotent re-index)."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO files(path, language, indexed_at, mtime) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET language=excluded.language, "
                "indexed_at=excluded.indexed_at, mtime=excluded.mtime",
                (file_path, language, _now(), mtime),
            )
            self._conn.execute("DELETE FROM symbols WHERE file_path=?", (file_path,))
            self._conn.execute("DELETE FROM imports WHERE file_path=?", (file_path,))
            self._conn.executemany(
                "INSERT INTO symbols(name, kind, qualified_name, signature, start_byte, "
                "end_byte, start_line, end_line, file_path, language) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        s.name,
                        s.kind,
                        s.qualified_name,
                        s.signature,
                        s.start_byte,
                        s.end_byte,
                        s.start_line,
                        s.end_line,
                        s.file_path,
                        s.language,
                    )
                    for s in symbols
                ],
            )
            self._conn.executemany(
                "INSERT INTO imports(target, file_path, language, line) VALUES(?,?,?,?)",
                [(i.target, i.file_path, i.language, i.line) for i in imports],
            )

    def file_mtime(self, file_path: str) -> Optional[float]:
        with self._lock:
            row = self._conn.execute("SELECT mtime FROM files WHERE path=?", (file_path,)).fetchone()
        return row[0] if row else None

    def drop_file(self, file_path: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM files WHERE path=?", (file_path,))
            self._conn.execute("DELETE FROM symbols WHERE file_path=?", (file_path,))
            self._conn.execute("DELETE FROM imports WHERE file_path=?", (file_path,))

    # ---- querying ----

    def search_symbols(
        self,
        query: str,
        kind: Optional[str] = None,
        language: Optional[str] = None,
        limit: int = _SEARCH_LIMIT_DEFAULT,
    ) -> List[SearchResult]:
        """Substring match on name (and qualified name).

        LIKE metacharacters in the query are escaped and `limit` is clamped to
        a bounded range so a single request can never dump the whole index.
        """
        query = _escape_like(str(query or ""))
        limit = _clamp_limit(limit)
        like = f"%{query}%"
        sql = "SELECT id, name, kind, qualified_name, signature, start_byte, end_byte, " \
              "start_line, end_line, file_path, language FROM symbols " \
              "WHERE (name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')"
        args: list = [like, like]
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        if language:
            sql += " AND language=?"
            args.append(language)
        sql += " ORDER BY name LIMIT ?"
        args.append(limit)
        with self._lock:
            return [_to_result(r) for r in self._conn.execute(sql, args).fetchall()]

    def symbol_by_id(self, symbol_id: int) -> List[SearchResult]:
        """Look up a single symbol by primary key (thread-safe)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, kind, qualified_name, signature, start_byte, end_byte, "
                "start_line, end_line, file_path, language FROM symbols WHERE id=?",
                (symbol_id,),
            ).fetchone()
        return [_to_result(row)] if row else []

    def exact_symbol(self, name: str, file_path: Optional[str] = None) -> List[SearchResult]:
        sql = "SELECT id, name, kind, qualified_name, signature, start_byte, end_byte, " \
              "start_line, end_line, file_path, language FROM symbols WHERE name=?"
        args: list = [name]
        if file_path:
            sql += " AND file_path=?"
            args.append(file_path)
        sql += " ORDER BY file_path LIMIT 100"
        with self._lock:
            return [_to_result(r) for r in self._conn.execute(sql, args).fetchall()]

    def symbols_in_file(self, file_path: str) -> List[SearchResult]:
        sql = "SELECT id, name, kind, qualified_name, signature, start_byte, end_byte, " \
              "start_line, end_line, file_path, language FROM symbols WHERE file_path=? " \
              "ORDER BY start_line"
        with self._lock:
            return [_to_result(r) for r in self._conn.execute(sql, (file_path,)).fetchall()]

    def importers_of(self, target: str) -> List[str]:
        """Files that import a target (exact or suffix match on the module path)."""
        raw = str(target or "")
        escaped = _escape_like(raw)
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT file_path FROM imports "
                "WHERE target=? OR target LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\' "
                "LIMIT ?",
                (raw, f"%/{escaped}", f"%.{escaped}", _SEARCH_LIMIT_MAX),
            ).fetchall()
        return [r[0] for r in rows]

    def imports_of(self, file_path: str) -> List[Tuple[str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT target, line FROM imports WHERE file_path=? ORDER BY line",
                (file_path,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            n_files = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            n_symbols = self._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            n_imports = self._conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
            by_lang = self._conn.execute(
                "SELECT language, COUNT(*) FROM symbols GROUP BY language ORDER BY COUNT(*) DESC"
            ).fetchall()
        return {
            "files": n_files,
            "symbols": n_symbols,
            "imports": n_imports,
            "symbols_by_language": dict(by_lang),
        }

    def indexed_files(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute("SELECT path FROM files ORDER BY path").fetchall()
        return [r[0] for r in rows]


def _now() -> float:
    import time

    return time.time()


def _to_result(row) -> SearchResult:
    (
        sid, name, kind, qname, sig, sb, eb, sl, el, fp, lang,
    ) = row
    return SearchResult(
        id=sid,
        symbol=Symbol(
            name=name,
            kind=kind,
            qualified_name=qname,
            signature=sig,
            start_byte=sb,
            end_byte=eb,
            start_line=sl,
            end_line=el,
            file_path=fp,
            language=lang,
        ),
    )
