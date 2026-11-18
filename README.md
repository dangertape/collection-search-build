# Collection Search

A Windows app that lets Claude search your documents by meaning. Claude only sees the collections you have switched on in the app. Filtering happens in the app's code, so Claude never receives results from collections that are off.

Full design: *Collection Search for Claude — Design* in the project files.

## What it does

- **Collections.** A collection is a set of folders plus filters: excluded subfolders, file types, a modified-date range and filename patterns. Each one has an on/off checkbox in the window and in the tray menu.
- **Folders and documents on or off.** On the **Collections** tab each collection opens like Explorer onto its folders and documents, each with its own checkbox. Untick a folder or a document to leave it out of that collection; everything inside an unticked folder goes with it, and you can tick single documents back on inside it. A half-filled box means some of what is inside is off, and the **Documents on** column says how many are on, such as `5 of 8`. The settings are saved straight away in `settings.json` and apply on Claude's next call: off documents drop out of search, Claude's tools, the Similar tab and the Map. They stay in the index, so switching them back on is instant. To stop a folder being indexed at all, add it under **Subfolders to skip** in the collection's settings instead. Unticking the collection itself keeps its folder and document settings for when you tick it again.
- **Search.** The app indexes PDF, Word, EPUB, web page (.html, .htm), text and Markdown files on your PC. It ranks passages by meaning (a local embedding model, no cloud) and by exact words, then combines the two rankings.
- **Search in the app.** The **Search** tab takes a question, a topic or exact words and lists the best passages, ranked the same way Claude's searches are. The box next to it picks where to look: the ticked collections (what Claude sees), all collections, or one collection, including one that isn't ticked. Searching an unticked collection here doesn't show it to Claude. Double-click a result to open the file, use **Find similar** to list documents like it on the **Similar** tab, or right-click for **Show in folder** and **Copy path**. **More results** loads the next page.
- **Similar documents.** Pick a document and the app lists the documents most like it, with the passages that match. It works in two layers. Each file has one whole-document vector (the average of its passage vectors), which finds files about the same thing overall. Some of the file's passages are also searched on their own, which finds files that share one strong section but are otherwise about something else. Each result's score mixes the two, and its best matching passages are shown next to the part of your file they match. Use the **Similar** tab, or ask Claude to find documents like one it found.
- **Map.** The **Map** tab draws every document in the ticked collections as a dot, with documents about the same thing grouped into named themes. Click a theme to list its documents, most typical first, or click a dot to see the documents next to it. Double-click to open a file, or use **Find similar** to jump to the Similar tab. Drag to move and scroll to zoom. The map uses the document vectors the index already has, so it embeds nothing and reads no files, and 35,000 documents draw in a few seconds. It redraws when you tick or untick a collection or when indexing finishes with changes.
- **Headings in results.** For Markdown and Word files, each passage carries the headings above it, such as `Exodus 20 > 4-6`, so Claude can cite it without opening the file. In an EPUB e-book the top heading is the chapter title from the book's table of contents, with any headings inside the chapter below it. E-books with DRM can't be read and are listed on the Status tab. Existing collections pick up EPUB files automatically after the upgrade; untick **.epub** in a collection to leave them out.
- **Web pages.** For .html and .htm files the app reads the page's own text and leaves out scripts, styles, menus, cookie banners, sidebars, forms and footers. When the page marks its content with `<main>` or `<article>`, only that part is read. Each paragraph carries the headings above it like Markdown, a page without a top heading uses its title instead, and table rows read as `cell | cell`. Existing collections pick up .html and .htm files automatically after the upgrade; untick them in a collection to leave them out.
- **Links and verse references.** While indexing, the app records `[[wikilinks]]`, Markdown links and web-page links to other files on your PC, and Bible verse references such as `Exod 20:4` or `1 Cor 8:4-6`. A chapter file named like `Exod-20.md` or `Exodus 20.md` counts as linked from every note that mentions a verse in that chapter.
- **Tools for Claude.** The connector is called **my-documents** in Claude, and every tool name says "document" so Claude doesn't confuse it with other apps' "collections":
  - `search_documents`: passages by meaning and exact words, 15 by default and up to 50, at most 3 per file per page, with `offset` for the next page.
  - `read_document`: a whole file, or with `around` just the part at a result's location or heading plus a few sections either side.
  - `find_similar_documents`: documents like one document.
  - `get_document_links`: what a document links to, what links back to it, and the verses it mentions.
  - `explore_documents`: starts from a query or a file and follows links, backlinks, verse references and similar documents one to three steps out, in one call.
  - `list_document_collections`: the collections that are switched on.

  Search, explore, find-similar and links take an optional `collection` name to look in just one collection. Only switched-on collections can be named, so this only ever narrows the scope; Claude can't widen it. Every tool is marked read-only, which Claude Desktop uses to group them in its tool permissions. Each tool has its own on/off switch. A tool that is off returns a short error on Claude's next call, in the same chat.
- **Add-on tools.** Drop a `.py` file into a `tools` folder and restart. See [Writing an add-on tool](#writing-an-add-on-tool).
- **Graphics card.** The search model runs on the graphics card through DirectML when the PC has one (any DirectX 12 card: Nvidia, AMD or Intel), and on the CPU otherwise. While it indexes, the app reads the next files in the background and sends passages from several files to the model at once, so the card stays busy. The Status tab says where the model runs. To keep it on the CPU, set `"use_gpu": false` under `preferences` in `settings.json` and restart the app.
- **Search model.** The default model is `BAAI/bge-base-en-v1.5`, an English model. Versions before 0.3 used the smaller `bge-small-en-v1.5`. Upgrading switches to the new model and re-indexes everything once, automatically. To pick another [fastembed model](https://qdrant.github.io/fastembed/examples/Supported_Models/), set `model` under `preferences` in `settings.json`. Changing the model always re-indexes.
- **Background indexing.** The app watches the folders for changes, rescans every 10 minutes to catch anything missed, and lists files it couldn't read on the Status tab.

## Install and connect (Windows)

1. Build the program folder, or use one someone built for you (see [Building](#building)).
2. Run `CollectionSearch.exe`. The window opens and a tray icon appears. The first start downloads the search model (about 210 MB) unless the build bundled it.
3. On the **Collections** tab, click **Add…**, pick your folders and filters, and save. Indexing starts in the background, and the **Status** tab shows progress.
4. On the **Status** tab, click **Add to Claude Desktop**, then quit and reopen Claude Desktop. This adds the entry below to `%APPDATA%\Claude\claude_desktop_config.json` and backs up the old file first:

   ```json
   { "mcpServers": { "my-documents": { "command": "C:\\...\\CollectionSearch\\collection-search-mcp.exe" } } }
   ```

5. Ask Claude about your documents.
6. The first time Claude uses each tool, Claude Desktop asks for permission. Choose **Always allow** to stop it asking again. The tools only read, and only from collections that are switched on.

Upgrading from a version that showed up in Claude as `collection-search`: click **Add to Claude Desktop** again. It replaces the old entry with `my-documents`. Claude Desktop treats it as a new connector, so choose **Always allow** once more. The first start also re-indexes everything once, to add headings and links.

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
pip uninstall -y onnxruntime; pip install --no-deps onnxruntime-directml  # optional: run the model on the graphics card
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

The parameters after `ctx` become the tool's inputs. `ctx` has `search(query, limit, offset)`, `read_file(path, around, context)`, `find_similar(path, limit)`, `document_links(path)`, `explore(query, path)` and `active_collections()`, and all of them are already limited to the active collections. An optional `TITLE` sets the name shown in Claude's permission prompts. Return material, not instructions. Claude treats instructions inside tool results with caution.

## Code layout

| Module | Responsibility |
| --- | --- |
| `parsers/` | PDF, .docx, .txt and .md to text sections with page or paragraph locations |
| `chunking.py` | Overlapping chunks on paragraph boundaries |
| `embeddings.py` | fastembed (ONNX; DirectML graphics card, else CPU) behind a small interface |
| `store.py` | SQLite holding files, chunks, passage and document vectors (sqlite-vec) and FTS5 keywords |
| `collections.py` | Collection rules: index-time (folders, types) and search-time (dates, patterns) |
| `indexer.py` | Diff by path, size and mtime; parse, chunk, embed, store |
| `links.py` | Finds links and Bible verse references in a file's text, and names Bible chapter files |
| `search.py` | Scoped hybrid search with paging, `find_similar`, `read_file`, links and explore; always receives the active collections |
| `similarity_map.py` | Map layout: k-means themes, theme placement, document positions and theme names |
| `settings.py` | JSON settings, one lock, change notifications |
| `engine.py` | Model, per-thread index connections, indexer thread, periodic rescan |
| `watcher.py` | Folder watching with debounce |
| `addons.py`, `tools/` | Tool format, loader and built-in tools |
| `mcp_server.py` | MCP server on localhost, token check, tool gating |
| `shim.py` | Stdio-to-localhost forwarder for Claude Desktop |
| `claude_config.py` | Adds the shim to Claude Desktop's config |
| `ui/` | PySide6 window, collection editor, Map tab and tray |
