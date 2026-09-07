"""Language registry and tree-sitter parser setup.

Maps file extensions to language names and builds a `Language` + `Parser`
for each. Symbol extraction understands per-language node types so one
extractor can walk Python, JS/TS, Go, Rust, and C/C++ trees uniformly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from tree_sitter import Language, Parser

import tree_sitter_python
import tree_sitter_javascript
import tree_sitter_typescript
import tree_sitter_go
import tree_sitter_rust
import tree_sitter_cpp


@dataclass(frozen=True)
class LangSpec:
    """Node-type mapping for a single language.

    `def_types` lists node types that declare a named symbol. `name_fields`
    is the ordered list of field names to try when reading a symbol's name
    (tree-sitter stores the identifier under a field that varies by language).
    """

    name: str
    extensions: tuple[str, ...]
    language_fn: object
    def_types: tuple[str, ...]
    name_fields: tuple[str, ...]
    # node types that represent an import / include / use statement
    import_types: tuple[str, ...] = ()


_SPECS: List[LangSpec] = [
    LangSpec(
        name="python",
        extensions=(".py", ".pyw"),
        language_fn=tree_sitter_python.language,
        def_types=(
            "function_definition",
            "class_definition",
        ),
        name_fields=("name",),
        import_types=("import_statement", "import_from_statement"),
    ),
    LangSpec(
        name="javascript",
        extensions=(".js", ".jsx", ".mjs", ".cjs"),
        language_fn=tree_sitter_javascript.language,
        def_types=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "generator_function_declaration",
            "lexical_declaration",
            "variable_declaration",
        ),
        name_fields=("name", "declarator"),
        import_types=("import_statement",),
    ),
    LangSpec(
        name="typescript",
        extensions=(".ts", ".tsx"),
        language_fn=tree_sitter_typescript.language_typescript,
        def_types=(
            "function_declaration",
            "class_declaration",
            "method_definition",
            "generator_function_declaration",
            "lexical_declaration",
            "variable_declaration",
            "interface_declaration",
            "type_alias_declaration",
            "enum_declaration",
        ),
        name_fields=("name", "declarator"),
        import_types=("import_statement",),
    ),
    LangSpec(
        name="go",
        extensions=(".go",),
        language_fn=tree_sitter_go.language,
        def_types=(
            "function_declaration",
            "method_declaration",
            "type_declaration",
            "var_declaration",
            "const_declaration",
        ),
        name_fields=("name", "declarator"),
        import_types=("import_declaration",),
    ),
    LangSpec(
        name="rust",
        extensions=(".rs",),
        language_fn=tree_sitter_rust.language,
        def_types=(
            "function_item",
            "struct_item",
            "enum_item",
            "trait_item",
            "impl_item",
            "const_item",
            "static_item",
            "type_item",
        ),
        name_fields=("name",),
        import_types=("use_declaration",),
    ),
    LangSpec(
        name="cpp",
        extensions=(".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"),
        language_fn=tree_sitter_cpp.language,
        def_types=(
            "function_definition",
            "class_specifier",
            "struct_specifier",
            "enum_specifier",
        ),
        name_fields=("name", "declarator"),
        import_types=("preproc_include",),
    ),
]

_LANG_BY_EXT: Dict[str, LangSpec] = {}
for _s in _SPECS:
    for _ext in _s.extensions:
        _LANG_BY_EXT[_ext] = _s

_LANGUAGES: Dict[str, "Language"] = {}
_PARSERS: Dict[str, Parser] = {}

for _s in _SPECS:
    try:
        _lang = Language(_s.language_fn())
    except Exception:
        # A grammar that fails to load is skipped rather than crashing the
        # whole server; languages are loaded lazily in get_parser anyway.
        continue
    _LANGUAGES[_s.name] = _lang
    _p = Parser(_lang)
    _PARSERS[_s.name] = _p


def spec_for_path(path: str) -> Optional[LangSpec]:
    """Return the LangSpec for a file path, or None if unsupported."""
    ext = os.path.splitext(path)[1].lower()
    return _LANG_BY_EXT.get(ext)


def get_parser(name: str) -> Optional[Parser]:
    """Return a cached Parser for a language name, or None if unavailable."""
    return _PARSERS.get(name)


def supported_languages() -> List[str]:
    """Return the language names that loaded successfully."""
    return sorted(_LANGUAGES.keys())


def language_for_extension(ext: str) -> Optional[str]:
    spec = _LANG_BY_EXT.get(ext.lower())
    return spec.name if spec else None
