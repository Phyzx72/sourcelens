"""sourcelens MCP server — token-efficient code retrieval.

Exposes tools for indexing a folder and querying exact symbols. Runs over
stdio by default (the standard MCP transport) and optionally over HTTP.

Tool surface (mirrors the jcodemunch value prop, built from scratch):
  index_folder      — parse + index every supported file under a path
  search_symbols    — find symbols by name (substring), optional kind/language
  get_symbol_source — fetch the exact source of a symbol by id or name+file
  get_file_outline  — list symbols declared in a file
  find_importers    — which files import a given module/target
  find_imports      — what a file imports
  stats             — index counts
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent, Tool

from . import __version__
from .indexer import walk
from .languages import get_parser, spec_for_path, supported_languages
from .store import Store

_DEFAULT_DB = os.path.expanduser("~/.sourcelens/index.db")
_IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
    "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "dist",
    "build", "target", ".next", ".cache",
}

mcp = MCPServer(
    name="sourcelens",
    title="sourcelens — token-efficient code retrieval",
    description="Local code index over tree-sitter ASTs. Index once, query exact symbols instead of re-reading files.",
    version=__version__,
)

_store: Optional[Store] = None


def _db_path() -> str:
    return os.environ.get("SOURCELENS_DB", _DEFAULT_DB)


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store(_db_path())
    return _store


def _iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


@mcp.tool()
def index_folder(path: str) -> dict:
    """Index every supported source file under `path` (absolute or relative).

    Walks the tree, parses each file with tree-sitter, and stores symbols +
    imports in the local SQLite index. Safe to re-run; files are replaced
    idempotently by path. Returns counts of what was indexed.
    """
    root = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(root):
        return {"error": f"not a directory: {root}"}

    store = get_store()
    indexed = 0
    skipped = 0
    by_lang: dict = {}
    for fp in _iter_files(root):
        spec = spec_for_path(fp)
        if spec is None:
            skipped += 1
            continue
        parser = get_parser(spec.name)
        if parser is None:
            skipped += 1
            continue
        try:
            with open(fp, "rb") as fh:
                source = fh.read()
            mtime = os.path.getmtime(fp)
            extraction = walk(parser, spec, source, fp)
            store.replace_file(fp, spec.name, mtime, extraction.symbols, extraction.imports)
            indexed += 1
            by_lang[spec.name] = by_lang.get(spec.name, 0) + 1
        except (OSError, UnicodeError):
            skipped += 1

    return {
        "indexed": indexed,
        "skipped": skipped,
        "files_by_language": by_lang,
        "stats": store.stats(),
    }


@mcp.tool()
def search_symbols(
    query: str,
    kind: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Search indexed symbols by name substring.

    Returns each match with its qualified name, kind, language, file path, and
    line range. Use get_symbol_source to fetch exact body text. `kind` filters
    to e.g. function/class/method; `language` to python/go/typescript/etc.
    """
    store = get_store()
    results = store.search_symbols(query, kind=kind, language=language, limit=limit)
    return {
        "count": len(results),
        "results": [
            {
                "id": r.id,
                "name": r.symbol.name,
                "qualified_name": r.symbol.qualified_name,
                "kind": r.symbol.kind,
                "language": r.symbol.language,
                "signature": r.symbol.signature,
                "file": r.symbol.file_path,
                "lines": [r.symbol.start_line, r.symbol.end_line],
            }
            for r in results
        ],
    }


def _read_exact(file_path: str, start_byte: int, end_byte: int) -> str:
    with open(file_path, "rb") as fh:
        fh.seek(start_byte)
        data = fh.read(end_byte - start_byte)
    return data.decode("utf-8", errors="replace")


@mcp.tool()
def get_symbol_source(symbol_id: Optional[int] = None, name: Optional[str] = None, file: Optional[str] = None) -> dict:
    """Fetch a symbol's exact source by id, or by name (+optional file).

    Returns the precise byte range from disk (no whole-file read), so an agent
    gets only the function/class body it needs. Provide symbol_id OR name.
    """
    store = get_store()
    results = []
    if symbol_id is not None:
        # id path: look up by primary key directly
        row = store._conn.execute(
            "SELECT id, name, kind, qualified_name, signature, start_byte, end_byte, "
            "start_line, end_line, file_path, language FROM symbols WHERE id=?",
            (symbol_id,),
        ).fetchone()
        if row:
            from .store import _to_result
            results = [_to_result(row)]
    elif name:
        results = store.exact_symbol(name, file_path=file)

    if not results:
        return {"error": "no symbol found", "count": 0}

    out = []
    for r in results:
        try:
            source = _read_exact(r.symbol.file_path, r.symbol.start_byte, r.symbol.end_byte)
        except OSError as e:
            source = f"<unreadable: {e}>"
        out.append(
            {
                "id": r.id,
                "name": r.symbol.name,
                "qualified_name": r.symbol.qualified_name,
                "kind": r.symbol.kind,
                "language": r.symbol.language,
                "file": r.symbol.file_path,
                "lines": [r.symbol.start_line, r.symbol.end_line],
                "source": source,
            }
        )
    return {"count": len(out), "results": out}


@mcp.tool()
def get_file_outline(file: str) -> dict:
    """List every symbol declared in a file, in source order.

    Use this instead of reading a whole file — it returns the outline
    (name, kind, line range, signature) so the agent can then pull only the
    symbol bodies it actually needs.
    """
    store = get_store()
    results = store.symbols_in_file(file)
    return {
        "file": file,
        "count": len(results),
        "symbols": [
            {
                "id": r.id,
                "name": r.symbol.name,
                "qualified_name": r.symbol.qualified_name,
                "kind": r.symbol.kind,
                "signature": r.symbol.signature,
                "lines": [r.symbol.start_line, r.symbol.end_line],
            }
            for r in results
        ],
    }


@mcp.tool()
def find_importers(target: str) -> dict:
    """Find files that import the given module/target.

    Matches exact or suffix module paths (e.g. "os", "fmt", "utils.foo").
    """
    store = get_store()
    files = store.importers_of(target)
    return {"target": target, "count": len(files), "files": files}


@mcp.tool()
def find_imports(file: str) -> dict:
    """List what a single file imports, in source order."""
    store = get_store()
    imports = store.imports_of(file)
    return {"file": file, "count": len(imports), "imports": [{"target": t, "line": l} for t, l in imports]}


@mcp.tool()
def stats() -> dict:
    """Return index stats: file count, symbol count, per-language breakdown."""
    store = get_store()
    return store.stats()


@mcp.tool()
def list_languages() -> dict:
    """Return the languages this indexer supports."""
    return {"languages": supported_languages()}


def main() -> None:
    """Entry point: run the server over stdio (or HTTP if SOURCELENS_HTTP is set)."""
    if os.environ.get("SOURCELENS_HTTP"):
        import asyncio

        host = os.environ.get("SOURCELENS_HOST", "127.0.0.1")
        port = int(os.environ.get("SOURCELENS_PORT", "8766"))
        asyncio.run(mcp.run_streamable_http_async(host=host, port=port))
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
