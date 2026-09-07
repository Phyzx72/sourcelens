import os
import tempfile

from sourcelens.indexer import walk
from sourcelens.languages import get_parser, spec_for_path
from sourcelens.store import Store


PY_SRC = b'''
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
'''


def _index_py(store: Store, path: str, source: bytes):
    spec = spec_for_path(path)
    parser = get_parser(spec.name)
    ex = walk(parser, spec, source, path)
    store.replace_file(path, spec.name, 1.0, ex.symbols, ex.imports)
    return ex


def test_extracts_classes_methods_and_functions():
    spec = spec_for_path("foo.py")
    parser = get_parser(spec.name)
    ex = walk(parser, spec, PY_SRC, "foo.py")
    names = {s.name: s for s in ex.symbols}
    assert "Point" in names
    assert names["Point"].kind == "class"
    assert "norm" in names
    assert names["norm"].kind == "method"
    assert names["norm"].qualified_name == "Point.norm"
    assert "make_point" in names
    assert names["make_point"].kind == "function"


def test_qualified_name_for_class_member():
    spec = spec_for_path("foo.py")
    parser = get_parser(spec.name)
    ex = walk(parser, spec, PY_SRC, "foo.py")
    norm = next(s for s in ex.symbols if s.name == "norm")
    assert norm.qualified_name == "Point.norm"


def test_extracts_imports():
    spec = spec_for_path("foo.py")
    parser = get_parser(spec.name)
    ex = walk(parser, spec, PY_SRC, "foo.py")
    targets = [i.target for i in ex.imports]
    assert "os" in targets
    assert "dataclasses" in targets


def test_store_roundtrip_search_and_source():
    db = tempfile.mktemp(suffix=".db")
    store = Store(db)
    try:
        _index_py(store, "foo.py", PY_SRC)
        # search by substring
        hits = store.search_symbols("make_poin")
        assert len(hits) == 1
        assert hits[0].symbol.name == "make_point"
        # exact source fetch by byte offsets
        r = hits[0].symbol
        body = PY_SRC[r.start_byte : r.end_byte].decode()
        assert body.startswith("def make_point")
        # file outline
        outline = store.symbols_in_file("foo.py")
        assert len(outline) == 3  # Point (class), norm (method), make_point (function)
        # importers
        importers = store.importers_of("os")
        assert "foo.py" in importers
    finally:
        store.close()
        os.unlink(db)


def test_store_idempotent_reindex():
    db = tempfile.mktemp(suffix=".db")
    store = Store(db)
    try:
        _index_py(store, "foo.py", PY_SRC)
        _index_py(store, "foo.py", PY_SRC)  # re-index same file
        hits = store.search_symbols("make_point")
        assert len(hits) == 1  # no duplicate rows
        assert store.stats()["files"] == 1
    finally:
        store.close()
        os.unlink(db)


def test_empty_or_unparseable_yields_nothing():
    spec = spec_for_path("foo.py")
    parser = get_parser(spec.name)
    ex = walk(parser, spec, b"", "foo.py")
    assert ex.symbols == []
    assert ex.imports == []
