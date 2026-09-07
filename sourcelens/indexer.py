"""Symbol extraction from tree-sitter parse trees.

Walks a parse tree and emits `Symbol` records for named definitions, plus
`Import` records for import/include/use statements. The extraction is
language-agnostic: per-language node types come from `languages.LangSpec`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from tree_sitter import Node

from .languages import LangSpec

# We cap the stored signature length; long bodies still get byte offsets so
# exact source is always retrievable on demand, but summaries stay cheap.
_MAX_SIG = 400

_KIND_BY_TYPE = {
    "function_definition": "function",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "function_item": "function",
    "method_definition": "method",
    "method_declaration": "method",
    "class_definition": "class",
    "class_declaration": "class",
    "class_specifier": "class",
    "struct_item": "struct",
    "struct_specifier": "struct",
    "enum_item": "enum",
    "enum_specifier": "enum",
    "enum_declaration": "enum",
    "trait_item": "trait",
    "interface_declaration": "interface",
    "impl_item": "impl",
    "type_alias_declaration": "type",
    "type_declaration": "type",
    "type_item": "type",
    "lexical_declaration": "variable",
    "variable_declaration": "variable",
    "var_declaration": "variable",
    "const_declaration": "constant",
    "const_item": "constant",
    "static_item": "static",
}


@dataclass
class Symbol:
    name: str
    kind: str
    qualified_name: str
    signature: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    file_path: str
    language: str


@dataclass
class Import:
    target: str
    file_path: str
    language: str
    line: int


@dataclass
class Extraction:
    symbols: List[Symbol] = field(default_factory=list)
    imports: List[Import] = field(default_factory=list)


def _node_text(n: Node, src: bytes) -> str:
    return src[n.start_byte : n.end_byte].decode("utf-8", errors="replace")


def _first_named_field(node: Node, fields: tuple[str, ...]) -> Optional[Node]:
    """Return the first named child stored under one of the given field names."""
    for f in fields:
        try:
            child = node.child_by_field_name(f)
        except Exception:
            child = None
        if child is not None and child.is_named:
            return child
    return None


def _read_name(node: Node, src: bytes, spec: LangSpec) -> Optional[str]:
    """Extract a symbol's declared name.

    Handles the two common shapes: a simple `name` field (Python/Go/Rust),
    and JS/TS/C++ `declarator` fields where the identifier is nested.
    """
    child = _first_named_field(node, spec.name_fields)
    if child is None:
        return None
    if child.type == "identifier":
        return _node_text(child, src)
    # For declarator fields, descend to the first identifier inside.
    ident = _find_first_type(child, ("identifier", "field_identifier", "type_identifier"))
    if ident is not None:
        return _node_text(ident, src)
    return _node_text(child, src)


def _find_first_type(node: Node, types: tuple[str, ...]) -> Optional[Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in types:
            return n
        stack.extend(reversed(n.children))
    return None


def _signature(node: Node, src: bytes) -> str:
    """A compact one-line-ish signature: source up to the first '{' or ':'."""
    text = _node_text(node, src)
    # Trim to the definition header (before the body brace or indented block).
    text = text.split("{", 1)[0].split(":", 1)[0].strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > _MAX_SIG:
        text = text[: _MAX_SIG - 3] + "..."
    return text


def _qualified_name(name: str, parents: List[str]) -> str:
    if parents:
        return ".".join(parents + [name])
    return name


def _extract_import(node: Node, src: bytes, spec: LangSpec, file_path: str) -> Optional[Import]:
    """Read the import target string from an import-type node."""
    text = _node_text(node, src)
    if spec.name == "python":
        if node.type == "import_statement":
            m = re.search(r"import\s+([\w.]+)", text)
            return Import(m.group(1), file_path, spec.name, node.start_point[0] + 1) if m else None
        m = re.search(r"from\s+([\w.]+)", text)
        return Import(m.group(1), file_path, spec.name, node.start_point[0] + 1) if m else None
    if spec.name == "go":
        # import "fmt" or import ("fmt"; "os")
        for child in node.children:
            if child.type == "import_spec":
                path_node = child.child_by_field_name("path")
                if path_node is not None:
                    raw = _node_text(path_node, src).strip('"`')
                    return Import(raw, file_path, spec.name, child.start_point[0] + 1)
        return None
    if spec.name == "cpp":
        # #include <...> or #include "..."
        m = re.search(r"#include\s*[<\"]([^>\"]+)", text)
        return Import(m.group(1), file_path, spec.name, node.start_point[0] + 1) if m else None
    if spec.name in ("javascript", "typescript"):
        # import ... from "..." / require("...")
        m = re.search(r"from\s*[\"']([^\"']+)", text)
        if m:
            return Import(m.group(1), file_path, spec.name, node.start_point[0] + 1)
        m = re.search(r"require\s*\(\s*[\"']([^\"']+)", text)
        if m:
            return Import(m.group(1), file_path, spec.name, node.start_point[0] + 1)
        return None
    if spec.name == "rust":
        # use std::io; / use crate::foo::Bar;
        m = re.search(r"use\s+([\w:]+)", text)
        return Import(m.group(1), file_path, spec.name, node.start_point[0] + 1) if m else None
    return None


def walk(parser, spec: LangSpec, source: bytes, file_path: str) -> Extraction:
    """Parse source and walk the tree, collecting symbols and imports."""
    out = Extraction()
    tree = parser.parse(source)
    root = tree.root_node
    if root.type == "ERROR":
        # Unparseable file: yield nothing rather than garbage.
        return out

    def visit(node: Node, parents: List[str]):
        node_type = node.type
        if node_type in spec.import_types:
            imp = _extract_import(node, source, spec, file_path)
            if imp is not None:
                out.imports.append(imp)

        pushed = False
        if node_type in spec.def_types:
            name = _read_name(node, source, spec)
            if name:
                kind = _KIND_BY_TYPE.get(node_type, "symbol")
                # A function defined inside a class scope is a method.
                if kind == "function" and parents:
                    kind = "method"
                out.symbols.append(
                    Symbol(
                        name=name,
                        kind=kind,
                        qualified_name=_qualified_name(name, parents),
                        signature=_signature(node, source),
                        start_byte=node.start_byte,
                        end_byte=node.end_byte,
                        start_line=node.start_point[0] + 1,
                        end_line=node.end_point[0] + 1,
                        file_path=file_path,
                        language=spec.name,
                    )
                )
                # A scope-introducing def opens a namespace for its members.
                if kind in ("class", "struct", "trait", "impl", "interface", "enum"):
                    parents.append(name)
                    pushed = True

        for child in node.children:
            visit(child, parents)

        if pushed:
            parents.pop()

    visit(root, [])
    return out
