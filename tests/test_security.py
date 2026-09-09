"""Regression tests for the three Strix findings.

- vuln-0001 (HIGH): missing auth on the HTTP endpoint -> bearer-token gate +
  refusal to bind a non-loopback interface without a token.
- vuln-0002 (MEDIUM): unbounded/wildcard search -> limit clamp + LIKE escaping.
- vuln-0003 (LOW): shared SQLite connection across threads -> lock + per-thread
  connection flag.
"""

import asyncio
import os
import tempfile
import threading

import pytest

from sourcelens.server import RequireBearerToken, main
from sourcelens.store import Store, _clamp_limit, _escape_like

PY_SRC = b"""
import os
import dataclasses


@dataclasses.dataclass
class Point:
    x: int
    y: int

    def norm(self) -> float:
        return (self.x ** 2 + self.y ** 2) ** 0.5


def make_point(x, y):
    return Point(x, y)
"""


def _make_indexed_store():
    from sourcelens.indexer import walk
    from sourcelens.languages import get_parser, spec_for_path

    db = tempfile.mktemp(suffix=".db")
    store = Store(db)
    spec = spec_for_path("foo.py")
    parser = get_parser(spec.name)
    ex = walk(parser, spec, PY_SRC, "foo.py")
    store.replace_file("foo.py", spec.name, 1.0, ex.symbols, ex.imports)
    return store, db


# ---- vuln-0002: LIKE escaping + limit clamp ----


def test_escape_like_neutralizes_wildcards():
    assert _escape_like("%") == "\\%"
    assert _escape_like("_") == "\\_"
    assert _escape_like("a\\b") == "a\\\\b"
    assert _escape_like("normal") == "normal"


def test_clamp_limit_bounds_values():
    assert _clamp_limit(-1) == 1
    assert _clamp_limit(0) == 1
    assert _clamp_limit(1) == 1
    assert _clamp_limit(50) == 50
    assert _clamp_limit(1000000) == 500
    assert _clamp_limit("abc") == 50
    assert _clamp_limit(None) == 50


def test_percent_query_is_not_a_match_all():
    store, db = _make_indexed_store()
    try:
        hits = store.search_symbols("%")
        assert hits == []  # no symbol name literally contains '%'
    finally:
        store.close()
        os.unlink(db)


def test_underscore_query_matches_literal_underscore_only():
    store, db = _make_indexed_store()
    try:
        hits = store.search_symbols("_")
        names = [h.symbol.name for h in hits]
        assert names == ["make_point"]
    finally:
        store.close()
        os.unlink(db)


def test_search_symbols_negative_limit_does_not_error():
    store, db = _make_indexed_store()
    try:
        hits = store.search_symbols("a", limit=-1)
        # "a" is a substring of make_point/Point signatures/names; bounded by clamp.
        assert len(hits) <= 500
    finally:
        store.close()
        os.unlink(db)


# ---- vuln-0003: thread safety ----


def test_concurrent_access_no_sqlite_threading_errors():
    store, db = _make_indexed_store()
    errors = []

    def worker():
        try:
            for _ in range(50):
                store.search_symbols("point")
                store.symbol_by_id(1)
                store.stats()
                store.importers_of("os")
                store.imports_of("foo.py")
                store.exact_symbol("make_point")
        except Exception as e:  # pragma: no cover - only recorded on failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []

    try:
        assert store.stats()["files"] == 1
        assert store.search_symbols("make_point")[0].symbol.name == "make_point"
    finally:
        store.close()
        os.unlink(db)


def test_importers_of_escapes_wildcards():
    store, db = _make_indexed_store()
    try:
        # '%' and '_' must not act as match-alls on the suffix LIKE patterns.
        assert store.importers_of("%") == []
        assert store.importers_of("_") == []
        # A normal suffix/exact match still resolves.
        assert "foo.py" in store.importers_of("os")
    finally:
        store.close()
        os.unlink(db)


def test_symbol_by_id_lookup():
    store, db = _make_indexed_store()
    try:
        hits = store.search_symbols("make_point")
        sid = hits[0].id
        got = store.symbol_by_id(sid)
        assert len(got) == 1
        assert got[0].symbol.name == "make_point"
        assert store.symbol_by_id(999999) == []
    finally:
        store.close()
        os.unlink(db)


# ---- vuln-0001: auth ----


class _FakeApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        body = b"ok"
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


def _scope(headers):
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }


async def _run_middleware(app, scope):
    captured = {}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
        elif message["type"] == "http.response.body":
            captured.setdefault("body", b"")
            captured["body"] += message["body"]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    await app(scope, receive, send)
    return captured


def test_middleware_rejects_missing_token():
    inner = _FakeApp()
    app = RequireBearerToken(inner, "secret")
    captured = asyncio.run(_run_middleware(app, _scope([])))
    assert captured["status"] == 401
    assert not inner.called


def test_middleware_rejects_wrong_token():
    inner = _FakeApp()
    app = RequireBearerToken(inner, "secret")
    captured = asyncio.run(
        _run_middleware(app, _scope([(b"authorization", b"Bearer wrong")]))
    )
    assert captured["status"] == 401
    assert not inner.called


def test_middleware_allows_correct_token():
    inner = _FakeApp()
    app = RequireBearerToken(inner, "secret")
    captured = asyncio.run(
        _run_middleware(app, _scope([(b"authorization", b"Bearer secret")]))
    )
    assert captured["status"] == 200
    assert inner.called


def test_main_refuses_non_loopback_without_token(monkeypatch):
    monkeypatch.setenv("SOURCELENS_HTTP", "1")
    monkeypatch.setenv("SOURCELENS_HOST", "0.0.0.0")
    monkeypatch.delenv("SOURCELENS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main()


def test_main_refuses_loopback_without_token(monkeypatch):
    # Default loopback bind is no longer a token-free path — stdio is.
    monkeypatch.setenv("SOURCELENS_HTTP", "1")
    monkeypatch.setenv("SOURCELENS_HOST", "127.0.0.1")
    monkeypatch.delenv("SOURCELENS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main()


def test_main_allows_non_loopback_with_token(monkeypatch):
    # With a token set, main() proceeds to asyncio.run(uvicorn). Patch the serve
    # helper to a no-op so we only assert the bind guard does NOT raise.
    monkeypatch.setenv("SOURCELENS_HTTP", "1")
    monkeypatch.setenv("SOURCELENS_HOST", "0.0.0.0")
    monkeypatch.setenv("SOURCELENS_TOKEN", "secret")
    import sourcelens.server as server_mod

    async def _noop_serve(app, host, port):
        return None

    monkeypatch.setattr(server_mod, "_serve_http", _noop_serve)
    # Must not raise SystemExit from the loopback/token guard.
    main()
