# Collection Search

A Windows app that lets Claude search your documents by meaning. Claude only sees the collections you have switched on in the app. Filtering happens in the app's code, so Claude never receives results from collections that are off.

Full design: *Collection Search for Claude — Design* in the project files.

## What it does

- **Collections.** A collection is a set of folders plus filters: excluded subfolders, file types, a modified-date range and filename patterns. Each one has an on/off checkbox in the window and in the tray menu.
- **Search.** The app indexes PDF, Word, text and Markdown files on your PC. It ranks passages by meaning (a local embedding model, no cloud) and by exact words, then combines the two rankings.
- **Similar documents.** Pick a document and the app lists the documents most like it, with the passages that match. It works in two layers. Each file has one whole-document vector (the average of its passage vectors), which finds files about the same thing overall. Some of the file's passages are also searched on their own, which finds files that share one strong section but are otherwise about something else. Each result's score mixes the two, and its best matching passages are shown next to the part of your file they match. Use the **Similar** tab, or ask Claude to find documents like one it found.
- **Tools for Claude.** `search`, `read_file`, `find_similar` and `list_collections`. None of them takes a collection argument, so Claude can't widen the scope. Each tool has its own on/off switch. A tool that is off returns a short error on Claude's next call, in the same chat.
- **Add-on tools.** Drop a `.py` file into a `tools` folder and restart. See [Writing an add-on tool](#writing-an-add-on-tool).
- **Background indexing.** The app watches the folders for changes, rescans every 10 minutes to catch anything missed, and lists files it couldn't read on the Status tab.

## Install and connect (Windows)

1. Build the program folder, or use one someone built for you (see [Building](#building)).
2. Run `CollectionSearch.exe`. The window opens and a tray icon appears. The first start downloads the search model (about 130 MB) unless the build bundled it.
3. On the **Collections** tab, click **Add…**, pick your folders and filters, and save. Indexing starts in the background, and the **Status** tab shows progress.
4. On the **Status** tab, click **Add to Claude Desktop**, then quit and reopen Claude Desktop. This adds the entry below to `%APPDATA%\Claude\claude_desktop_config.json` and backs up the old file first:

   ```json
   { "mcpServers": { "collection-search": { "command": "C:\\...\\CollectionSearch\\collection-search-mcp.exe" } } }
   ```

5. Ask Claude about your documents.

Closing the window hides it to the tray. Use **Quit** in the tray menu to stop the app. To start it at sign-in, choose that option in the installer, or put a shortcut to `CollectionSearch.exe --tray` in your Startup folder.

## How Claude reaches the app

Claude Desktop starts MCP servers itself over stdin/stdout, but this app is already running with its window and tray. So Claude Desktop launches the small `collection-search-mcp.exe` instead. That program forwards each message to the app on `127.0.0.1`. The app listens only on that local address and checks a random token from `%USERPROFILE%\.collection-search\connection.json`. If the app isn't running, Claude gets the message "Collection Search is not running" instead of a hang.

## Where things are stored

| What | Where |
| --- | --- |
| Settings (collections, tool switches) | `%USERPROFILE%\.collection-search\settings.json` |
| Connection file (port and token) | `%USERPROFILE%\.collection-search\connection.json`, removed when the app quits |
| Index | `%USERPROFILE%\.collection-search\index.db` |
| Search model | `%USERPROFILE%\.collection-search\models`, or a `models` folder next to the .exe |
| Log | `%USERPROFILE%\.collection-search\app.log` |

These are kept outside AppData because Claude Desktop is a packaged app: programs it starts see a private copy of AppData, so the app and its connector would otherwise disagree about where its files are.

Your document folders are never written to.

## Building

On Windows with Python 3.12:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1               # dist\CollectionSearch\
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -BundleModel  # include the model for offline use
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Installer    # also make an installer (needs Inno Setup 6)
```

The build is a PyInstaller folder build with two programs, and the PC running it doesn't need Python installed:

- `CollectionSearch.exe` is the app. It has no console window.
- `collection-search-mcp.exe` is the shim Claude Desktop launches. It's a separate console program because a windowed program may start without usable stdin/stdout.

## Running from source

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
python -m collection_search             # the app
python -m collection_search --mcp       # the shim, as Claude Desktop runs it
python -m collection_search cli --help  # command-line index/search/eval
pytest
```

The tests use a small offline word-hash embedder and run the UI without a screen. One test drives the real MCP client through the shim into the app's server.

## Writing an add-on tool

Put a file like this in `tools\` next to `CollectionSearch.exe`, or in `%USERPROFILE%\.collection-search\tools\`. Then restart the app. Claude sees the new tool in a new chat.

```python
from typing import Annotated
from pydantic import Field

NAME = "find_invoices"
DESCRIPTION = "Find invoices that mention a customer."


def handler(ctx, customer: Annotated[str, Field(description="Customer name")]) -> dict:
    hits = ctx.search(f"invoice {customer}", limit=10)
    return {"invoices": [h["path"] for h in hits]}
```

The parameters after `ctx` become the tool's inputs. `ctx` has only three methods, `search(query, limit)`, `read_file(path)` and `active_collections()`, and all three are already limited to the active collections. Return material, not instructions. Claude treats instructions inside tool results with caution.

## Code layout

| Module | Responsibility |
| --- | --- |
| `parsers/` | PDF, .docx, .txt and .md to text sections with page or paragraph locations |
| `chunking.py` | Overlapping chunks on paragraph boundaries |
| `embeddings.py` | fastembed (ONNX, CPU) behind a small interface |
| `store.py` | SQLite holding files, chunks, passage and document vectors (sqlite-vec) and FTS5 keywords |
| `collections.py` | Collection rules: index-time (folders, types) and search-time (dates, patterns) |
| `indexer.py` | Diff by path, size and mtime; parse, chunk, embed, store |
| `search.py` | Scoped hybrid search, `find_similar` and `read_file`; always receives the active collections |
| `settings.py` | JSON settings, one lock, change notifications |
| `engine.py` | Model, per-thread index connections, indexer thread, periodic rescan |
| `watcher.py` | Folder watching with debounce |
| `addons.py`, `tools/` | Tool format, loader and built-in tools |
| `mcp_server.py` | MCP server on localhost, token check, tool gating |
| `shim.py` | Stdio-to-localhost forwarder for Claude Desktop |
| `claude_config.py` | Adds the shim to Claude Desktop's config |
| `ui/` | PySide6 window, collection editor and tray |
