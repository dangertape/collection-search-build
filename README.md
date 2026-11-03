# Collection Search

A Windows app that lets Claude search your documents by meaning. It only searches the collections you have switched on in the app. Full design: *Collection Search for Claude — Design* in the project files.

## Status: phase 1, the search core

This phase covers parsing, chunking, embedding, storage and hybrid search, driven from a command-line script. There is no MCP server, window or tray yet.

| Module | What it does |
| --- | --- |
| `parsers/` | PDF (one section per page), .docx (paragraphs and tables), .txt and .md. Add a type by adding one parser module. |
| `chunking.py` | Splits text into about 800-character chunks with 150 characters of overlap, on paragraph boundaries. Each chunk records its page or paragraph range. |
| `embeddings.py` | Local embeddings with fastembed (default `BAAI/bge-small-en-v1.5`). |
| `store.py` | One SQLite file in `%LOCALAPPDATA%\CollectionSearch` holding files, chunks, vectors (sqlite-vec) and an FTS5 keyword index. |
| `collections.py` | Collections: folders, excluded subfolders, file types, date range, filename patterns. |
| `indexer.py` | Indexes new and changed files and drops removed ones, comparing path, size and modified time. |
| `search.py` | Semantic and keyword search merged with reciprocal rank fusion. Results are limited to the files that active collections allow. |

**How scope is enforced:** search first builds the set of allowed files from the active collections. Both the vector query and the keyword query are restricted to that set in SQL, so chunks from other files are never ranked. The tests in `tests/test_search.py` check this.

## Try it on Windows

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]

# Index a folder. The model (about 130 MB) downloads on first run.
python scripts\search_cli.py index "D:\Some\Documents"

# Search it
python scripts\search_cli.py search "notice period in the lease"
python scripts\search_cli.py search "INV-4471" --no-keywords   # compare against semantic only

# Show counts and files that could not be read (scans, password-protected PDFs)
python scripts\search_cli.py status
```

## Measuring search quality (the phase 1 gate)

Copy `questions.example.csv` to `questions.csv`. Write 20 to 30 questions about your own documents. For each one, add part of the filename that should answer it. Then run:

```powershell
python scripts\search_cli.py eval questions.csv
python scripts\search_cli.py eval questions.csv --no-keywords
```

The gate is the right file in the top 5 for at least 80% of questions.

To compare chunk sizes, index into a separate file with `--db`, for example:
`python scripts\search_cli.py --db C:\tmp\cs-1200\index.db index "D:\Docs" --chunk-size 1200 --overlap 200`

## Tests

```powershell
pytest
```

The tests use a small offline word-hash embedder (`--offline-test-embedder` on the CLI), so they need no model download. It is only for tests and not a real search model.
