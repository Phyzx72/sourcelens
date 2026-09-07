# sourcelens

**Token-efficient local code retrieval as an MCP server.** Index a codebase once with tree-sitter AST parsing, then query exact symbols — functions, classes, methods — instead of re-reading whole files into your context window.

Built from scratch, MIT-licensed. No paid license, no network, local-first.

## Why

Reading whole files to find one function is a token incinerator. `sourcelens` parses source into an AST once, stores each symbol's name, kind, qualified name, signature, and exact byte offsets, then serves precise bodies on demand. Pull only the function you need — not the 800-line module around it.

## Install

```bash
git clone <this-repo> sourcelens
cd sourcelens
uv venv .venv && source .venv/bin/activate
uv pip install -e .
```

Requires Python 3.10+.

## Run

Stdio (default MCP transport):

```bash
sourcelens          # or: python -m sourcelens.server
```

HTTP (for remote clients):

```bash
SOURCELENS_HTTP=1 SOURCELENS_PORT=8766 sourcelens
```

The index is a SQLite file at `~/.sourcelens/index.db` by default. Override with `SOURCELENS_DB=/path/to/index.db`.

## Tools

| Tool | What it does |
|------|--------------|
| `index_folder(path)` | Walk + parse every supported file, store symbols + imports |
| `search_symbols(query, kind?, language?)` | Find symbols by name substring |
| `get_symbol_source(id? / name?+file?)` | Fetch a symbol's exact body (byte-precise, no whole-file read) |
| `get_file_outline(file)` | List all symbols in a file, source order |
| `find_importers(target)` | Which files import a module |
| `find_imports(file)` | What a file imports |
| `stats()` | Index counts + per-language breakdown |
| `list_languages()` | Supported languages |

## Supported languages

Python (`.py`), JavaScript (`.js/.jsx`), TypeScript (`.ts/.tsx`), Go (`.go`), Rust (`.rs`), C/C++ (`.c/.h/.cpp/.hpp`).

## Usage pattern (the whole point)

1. `index_folder("/path/to/repo")` — once.
2. `search_symbols("handle_request")` — find it.
3. `get_symbol_source(name="handle_request")` — get *only* that function's body.

Never `Read` a whole file to find one symbol again.

## Tests

```bash
python -m pytest tests/ -v
```

## License

MIT. See `LICENSE`.
