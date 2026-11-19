"""The MCP server: tool list, gating, scope, and the stdio shim end to end."""

import json
import os
import subprocess
import sys
import time

import anyio
import pytest

from collection_search import shim
from collection_search.addons import load_tools
from collection_search.collections import Collection
from collection_search.embeddings import HashEmbedder
from collection_search.engine import Engine
from collection_search.mcp_server import HttpServer, build_server, disabled_message
from collection_search.settings import Settings

from conftest import write


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTION_SEARCH_HOME", str(tmp_path / "home"))
    docs = tmp_path / "docs"
    write(str(docs / "on" / "lease.txt"), "The notice period is 90 days. Invoice 4471-B.")
    write(str(docs / "off" / "secret.txt"), "Private zebracorn notes.")
    settings = Settings.load(str(tmp_path / "home" / "settings.json"))
    settings.preferences.watch_folders = False
    on = Collection("On", [str(docs / "on")])
    off = Collection("Off", [str(docs / "off")], active=False)
    settings.upsert_collection(on)
    settings.upsert_collection(off)
    engine = Engine(settings, str(tmp_path / "home" / "index.db"), lambda m: HashEmbedder())
    engine.start()
    end = time.time() + 10
    while engine.status().state != "idle" and time.time() < end:
        time.sleep(0.05)
    server = build_server(engine, load_tools())
    yield server, settings, engine, docs, off
    engine.stop()


class ErrorResult:
    """In-process call_tool raises ToolError; over the wire it becomes an is_error result."""

    def __init__(self, message):
        self.message = message


def call(server, name, args):
    from mcp.server.mcpserver.exceptions import ToolError

    async def go():
        try:
            return await server.call_tool(name, args)
        except ToolError as exc:
            return ErrorResult(str(exc))
    return anyio.run(go)


def text_of(result):
    return result.message if isinstance(result, ErrorResult) else result.content[0].text


def is_error(result):
    if isinstance(result, ErrorResult):
        return True
    return getattr(result, "is_error", None) or getattr(result, "isError", False)


def test_collection_parameter_only_on_scoped_tools(app):
    server, *_ = app

    async def go():
        return await server.list_tools()
    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"search_documents", "read_document", "list_document_collections", "list_document_folders",
                          "find_similar_documents", "get_document_links", "explore_documents"}
    narrowing = {"search_documents", "find_similar_documents", "get_document_links", "explore_documents",
                 "list_document_folders"}
    for t in tools.values():
        schema = t.input_schema if hasattr(t, "input_schema") else t.inputSchema
        props = schema.get("properties", {})
        assert ("collection" in props) == (t.name in narrowing)
        assert ("folder" in props) == (t.name in narrowing)
        assert ("file_name" in props) == (t.name in narrowing - {"list_document_folders"})
        assert not any("collection" in p for p in props if p != "collection")


def test_search_one_collection(app):
    server, settings, engine, docs, off = app
    write(str(docs / "two" / "other.txt"), "The notice period here is 30 days.")
    two = Collection("Second Set", [str(docs / "two")])
    settings.upsert_collection(two)
    engine.request_sync()
    end = time.time() + 10
    while time.time() < end:
        data = json.loads(text_of(call(server, "search_documents", {"query": "notice period", "limit": 10})))
        if any("other.txt" in r["path"] for r in data["results"]):
            break
        time.sleep(0.1)
    paths = {r["path"] for r in data["results"]}
    assert any("lease" in p for p in paths) and any("other" in p for p in paths)

    data = json.loads(text_of(call(server, "search_documents",
                                   {"query": "notice period", "limit": 10, "collection": " second set "})))
    assert data["results"] and all("other" in r["path"] for r in data["results"])
    assert all(r["collections"] == ["Second Set"] for r in data["results"])

    links = json.loads(text_of(call(server, "explore_documents", {"query": "notice period", "collection": "On"})))
    assert links["documents"] and all("lease" in d["path"] for d in links["documents"])


def test_collection_cannot_reach_switched_off_or_unknown(app):
    server, settings, engine, docs, off = app
    for name in ["Off", "Nope"]:
        result = call(server, "search_documents", {"query": "zebracorn", "collection": name})
        assert is_error(result)
        assert f'No switched-on collection is named "{name}"' in text_of(result)
        assert text_of(result).endswith('Switched-on collections: "On".')
    result = call(server, "find_similar_documents", {"path": str(docs / "off" / "secret.txt"), "collection": "On"})
    assert is_error(result) and "not in an active collection" in text_of(result)
    data = json.loads(text_of(call(server, "search_documents", {"query": "zebracorn", "collection": ""})))
    assert all("secret" not in r["path"] for r in data["results"])


def test_search_returns_only_active(app):
    server, settings, engine, docs, off = app
    data = json.loads(text_of(call(server, "search_documents", {"query": "zebracorn notes", "limit": 10})))
    assert all("secret" not in r["path"] for r in data["results"])
    settings.set_collection_active(off.id, True)
    data = json.loads(text_of(call(server, "search_documents", {"query": "zebracorn notes", "limit": 10})))
    assert any("secret" in r["path"] for r in data["results"])


def test_list_collections(app):
    server, *_ = app
    server, settings, engine, docs, off = app
    assert json.loads(text_of(call(server, "list_document_collections", {}))) == {
        "collections": [{"name": "On", "folders": [str(docs / "on")]}]}


@pytest.fixture
def tree(app):
    """A second switched-on collection with nested folders, one of them switched off."""
    server, settings, engine, docs, off = app
    root = docs / "library"
    write(str(root / "Part A" / "one.txt"), "The harbor lighthouse keeps its lamp lit.")
    write(str(root / "Part A" / "Sub" / "two.txt"), "A lighthouse keeper writes in the log every night.")
    write(str(root / "Part B" / "three.txt"), "The lighthouse on the far cape was rebuilt in stone.")
    write(str(root / "Hidden" / "four.txt"), "Secret lighthouse plans.")
    lib = Collection("Library", [str(root)], switches={str(root / "Hidden"): False})
    settings.upsert_collection(lib)
    engine.request_sync()
    end = time.time() + 10
    while time.time() < end:
        data = json.loads(text_of(call(server, "search_documents", {"query": "lighthouse", "limit": 10})))
        if len({r["path"] for r in data["results"]}) >= 3:
            break
        time.sleep(0.1)
    return server, root


def basenames(results, key="path"):
    return {os.path.basename(r[key]) for r in results}


def test_search_one_folder(tree):
    server, root = tree
    data = json.loads(text_of(call(server, "search_documents", {"query": "lighthouse", "limit": 10})))
    assert {"one.txt", "two.txt", "three.txt"} <= basenames(data["results"])
    for folder in [str(root / "Part A"), "Part A", " Part A/ "]:
        data = json.loads(text_of(call(server, "search_documents",
                                       {"query": "lighthouse", "limit": 10, "folder": folder})))
        assert basenames(data["results"]) == {"one.txt", "two.txt"}, folder
        assert all(r["collections"] == ["Library"] for r in data["results"])
    data = json.loads(text_of(call(server, "search_documents",
                                   {"query": "lighthouse", "collection": "library", "folder": "Part A/Sub"})))
    assert basenames(data["results"]) == {"two.txt"}


def test_folder_cannot_widen_the_scope(tree, app):
    server, root = tree
    docs = root.parent
    for folder in ["Hidden", str(root / "Hidden"), "../off", str(docs / "off"), "Nope", "Part A/../../off"]:
        result = call(server, "search_documents", {"query": "lighthouse zebracorn", "folder": folder})
        assert is_error(result) and "No folder" in text_of(result), folder
    # A folder in another collection doesn't match when a collection is named.
    result = call(server, "search_documents", {"query": "lighthouse", "collection": "On", "folder": "Part A"})
    assert is_error(result) and 'in the collection "On"' in text_of(result)
    # A folder above a top folder covers the collections inside it, switches still apply.
    data = json.loads(text_of(call(server, "search_documents",
                                   {"query": "lighthouse zebracorn", "limit": 20, "folder": str(docs)})))
    found = basenames(data["results"])
    assert "four.txt" not in found and "secret.txt" not in found and "one.txt" in found


def test_search_by_file_name(tree):
    server, root = tree

    def found(**args):
        data = json.loads(text_of(call(server, "search_documents", {"query": "lighthouse", "limit": 20, **args})))
        return basenames(data["results"])
    assert found(file_name="TWO") == {"two.txt"}
    assert found(file_name="t*.txt") == {"two.txt", "three.txt"}
    assert found(file_name="one, three") == {"one.txt", "three.txt"}
    assert found(file_name="o*", folder="Part A") == {"one.txt"}
    assert found(file_name="four") == set()  # switched off stays hidden
    assert found(file_name="nothing-like-this") == set()
    source = str(root / "Part A" / "one.txt")
    data = json.loads(text_of(call(server, "find_similar_documents", {"path": source, "file_name": "three"})))
    assert basenames(data["results"]) == {"three.txt"}


def test_name_matches():
    from collection_search.collections import name_matches
    assert name_matches("Meeting Summary.md", "summary")
    assert name_matches("a-notes.MD", "*-notes.md")
    assert not name_matches("notes.txt", "*.md")
    assert name_matches("b.txt", "a | b")
    assert not name_matches("x.txt", " , ")


def test_folder_on_similar_links_and_explore(tree):
    server, root = tree
    source = str(root / "Part B" / "three.txt")
    data = json.loads(text_of(call(server, "find_similar_documents", {"path": source, "folder": "Part A"})))
    assert basenames(data["results"]) <= {"one.txt", "two.txt"} and data["results"]
    data = json.loads(text_of(call(server, "explore_documents", {"path": source, "folder": "Part A"})))
    assert basenames(data["documents"]) <= {"one.txt", "two.txt", "three.txt"}
    data = json.loads(text_of(call(server, "explore_documents", {"query": "lighthouse", "folder": "Part B"})))
    assert basenames(data["documents"]) == {"three.txt"}
    data = json.loads(text_of(call(server, "get_document_links", {"path": source, "folder": "Part A"})))
    assert data["path"] == source
    # The starting file must still be switched on.
    result = call(server, "find_similar_documents", {"path": str(root / "Hidden" / "four.txt"), "folder": "Part A"})
    assert is_error(result) and "not in an active collection" in text_of(result)


def test_list_folders(tree):
    server, root = tree
    data = json.loads(text_of(call(server, "list_document_folders", {"collection": "Library"})))
    assert data["truncated"] is False
    [lib] = data["collections"]
    assert lib["name"] == "Library"
    listed = {f["path"]: (f["documents"], f["subfolders"]) for f in lib["folders"]}
    assert listed == {str(root): (3, 2), str(root / "Part A"): (2, 1),
                      str(root / "Part A" / "Sub"): (1, 0), str(root / "Part B"): (1, 0)}
    data = json.loads(text_of(call(server, "list_document_folders", {"collection": "Library", "depth": 0})))
    assert [f["path"] for f in data["collections"][0]["folders"]] == [str(root)]
    data = json.loads(text_of(call(server, "list_document_folders", {"folder": "Part A", "depth": 1})))
    assert [c["name"] for c in data["collections"]] == ["Library"]
    assert [f["path"] for f in data["collections"][0]["folders"]] == [str(root / "Part A"), str(root / "Part A" / "Sub")]
    data = json.loads(text_of(call(server, "list_document_folders", {})))
    assert {c["name"] for c in data["collections"]} == {"On", "Library"}


def test_disabled_tool_errors_on_next_call(app):
    server, settings, *_ = app
    settings.set_tool_enabled("search_documents", False)
    result = call(server, "search_documents", {"query": "notice"})
    assert is_error(result) and disabled_message("search_documents") in text_of(result)
    settings.set_tool_enabled("search_documents", True)
    assert not is_error(call(server, "search_documents", {"query": "notice"}))


def test_read_file_scope(app):
    server, settings, engine, docs, off = app
    ok = call(server, "read_document", {"path": str(docs / "on" / "lease.txt")})
    assert "notice period" in json.loads(text_of(ok))["text"]
    for path in [str(docs / "off" / "secret.txt"),
                 str(docs / "on" / ".." / "off" / "secret.txt"),
                 "/etc/passwd",
                 str(docs / "on" / "missing.txt")]:
        result = call(server, "read_document", {"path": path})
        assert is_error(result) and "not in an active collection" in text_of(result)


def test_read_file_through_symlink_outside_scope(app, tmp_path):
    server, settings, engine, docs, off = app
    link = docs / "on" / "link.txt"
    try:
        os.symlink(docs / "off" / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    result = call(server, "read_document", {"path": str(link)})
    assert is_error(result)


def test_limit_is_validated(app):
    server, *_ = app
    assert is_error(call(server, "search_documents", {"query": "x", "limit": 1000}))


def test_shim_end_to_end(app, tmp_path):
    """Official MCP client -> shim process (stdio) -> app's HTTP server."""
    server, settings, *_ = app
    http = HttpServer(server, str(tmp_path / "home" / "connection.json"), port=0)
    http.start()
    assert http.wait_started()
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "collection_search", "--mcp"],
            env={**os.environ, "COLLECTION_SEARCH_HOME": str(tmp_path / "home")},
        )

        async def go():
            async with stdio_client(params) as (read, write_):
                async with ClientSession(read, write_) as session:
                    await session.initialize()
                    names = sorted(t.name for t in (await session.list_tools()).tools)
                    found = await session.call_tool("search_documents", {"query": "4471-B", "limit": 3})
                    settings.set_tool_enabled("search_documents", False)
                    blocked = await session.call_tool("search_documents", {"query": "4471-B"})
                    return names, found, blocked

        names, found, blocked = anyio.run(go)
        assert names == ["explore_documents", "find_similar_documents", "get_document_links",
                         "list_document_collections", "list_document_folders", "read_document",
                         "search_documents"]
        assert "lease.txt" in found.content[0].text
        assert is_error(blocked) and "turned off" in blocked.content[0].text
    finally:
        http.stop()
    assert not os.path.exists(str(tmp_path / "home" / "connection.json"))


@pytest.fixture
def no_app(tmp_path, monkeypatch):
    """No app running; starting one is recorded instead of done."""
    monkeypatch.setenv("COLLECTION_SEARCH_HOME", str(tmp_path))
    monkeypatch.setattr(shim, "APP_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(shim, "_launched", False)
    launches = []
    monkeypatch.setattr(shim.subprocess, "Popen", lambda args, **kw: launches.append(args))
    return launches


def test_shim_reports_app_not_running(no_app):
    reply = json.loads(shim.forward(b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}'))
    assert reply["id"] == 7 and "not running" in reply["error"]["message"]
    assert shim.forward(b'{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
    assert json.loads(shim.forward(b"not json"))["error"]["code"] == -32700
    assert len(no_app) == 1  # tried to start the app, once


def test_shim_answers_initialize_without_the_app(no_app, tmp_path):
    """Claude's first message must never wait on the app, or Claude marks the connector failed."""
    start = time.time()
    reply = json.loads(shim.forward(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'))
    assert time.time() - start < 2
    assert reply["id"] == 1 and reply["result"]["protocolVersion"] == "2025-06-18"
    assert reply["result"]["capabilities"]["tools"] is not None
    assert json.loads(shim.forward(b'{"jsonrpc":"2.0","id":2,"method":"ping"}'))["result"] == {}
    assert no_app and (tmp_path / shim.LAUNCH_MARKER).exists()


def test_shim_uses_saved_answers_while_app_starts(app, tmp_path, no_app, monkeypatch):
    """Answers saved while the app was up are reused when it is away."""
    server, *_ = app
    http = HttpServer(server, str(tmp_path / "home" / "connection.json"), port=0)
    http.start()
    assert http.wait_started()
    monkeypatch.setenv("COLLECTION_SEARCH_HOME", str(tmp_path / "home"))
    try:
        init = b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18",' \
               b'"capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
        live_init = json.loads(shim.forward(init))
        live_tools = json.loads(shim.forward(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}'))
        assert "search_documents" in {t["name"] for t in live_tools["result"]["tools"]}
    finally:
        http.stop()
    saved_init = json.loads(shim.forward(init))
    assert saved_init["result"]["instructions"] == live_init["result"]["instructions"]
    saved_tools = json.loads(shim.forward(b'{"jsonrpc":"2.0","id":3,"method":"tools/list"}'))
    assert saved_tools["id"] == 3 and saved_tools["result"] == live_tools["result"]
    call = json.loads(shim.forward(b'{"jsonrpc":"2.0","id":4,"method":"tools/call",'
                                   b'"params":{"name":"search_documents","arguments":{"query":"x"}}}'))
    assert call["id"] == 4 and "error" in call  # app gone and not coming back in the test
    assert len(no_app) == 1


def test_shim_waits_for_a_starting_app(app, tmp_path, no_app, monkeypatch):
    """A tool call made while the app is starting is answered once it is up."""
    import threading

    server, *_ = app
    monkeypatch.setattr(shim, "APP_WAIT_SECONDS", 10)
    monkeypatch.setenv("COLLECTION_SEARCH_HOME", str(tmp_path / "home"))
    http = HttpServer(server, str(tmp_path / "home" / "connection.json"), port=0)
    timer = threading.Timer(1.5, lambda: (http.start(), http.wait_started()))
    timer.start()
    try:
        reply = json.loads(shim.forward(b'{"jsonrpc":"2.0","id":5,"method":"tools/call",'
                                        b'"params":{"name":"list_document_collections","arguments":{}}}'))
        assert reply["id"] == 5 and "result" in reply
    finally:
        timer.join()
        http.stop()


def test_wrong_token_rejected(app, tmp_path):
    import urllib.error
    import urllib.request

    server, *_ = app
    http = HttpServer(server, str(tmp_path / "home" / "connection.json"), port=0)
    http.start()
    assert http.wait_started()
    try:
        req = urllib.request.Request(http.url, data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 401
    finally:
        http.stop()


def test_find_similar_scope(app):
    server, settings, engine, docs, off = app
    write(str(docs / "on" / "lease2.txt"), "Lease notice period of 60 days. Invoice 4471-C.")
    write(str(docs / "off" / "lease_copy.txt"), "The notice period is 90 days. Invoice 4471-B.")
    engine.request_sync()
    end = time.time() + 10
    while time.time() < end and engine.status().files < 4:
        time.sleep(0.05)
    data = json.loads(text_of(call(server, "find_similar_documents", {"path": str(docs / "on" / "lease.txt")})))
    assert [os.path.basename(r["path"]) for r in data["results"]] == ["lease2.txt"]
    assert data["results"][0]["passages"] and data["count"] == 1
    result = call(server, "find_similar_documents", {"path": str(docs / "off" / "secret.txt")})
    assert is_error(result) and "not in an active collection" in text_of(result)
    settings.set_tool_enabled("find_similar_documents", False)
    result = call(server, "find_similar_documents", {"path": str(docs / "on" / "lease.txt")})
    assert is_error(result) and disabled_message("find_similar_documents") in text_of(result)


def test_tools_are_marked_read_only(app):
    server, *_ = app

    async def go():
        return await server.list_tools()
    for tool in anyio.run(go):
        hints = tool.annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False
        assert hints.open_world_hint is False and hints.title
        assert tool.title


def test_new_tools_over_mcp(app):
    server, settings, engine, docs, off = app
    write(str(docs / "on" / "notes.md"), "# Lease\n\n## Notice\n\nSee [[lease]] for the 90 days.\n\nMore text.")
    engine.request_sync()
    end = time.time() + 10
    while time.time() < end and engine.status().files < 3:
        time.sleep(0.05)
    data = json.loads(text_of(call(server, "search_documents", {"query": "notice", "limit": 1})))
    assert data["count"] == 1 and data["next_offset"] == 1
    links = json.loads(text_of(call(server, "get_document_links", {"path": str(docs / "on" / "notes.md")})))
    assert [os.path.basename(l["path"]) for l in links["links_to"]] == ["lease.txt"]
    part = json.loads(text_of(call(server, "read_document",
                                   {"path": str(docs / "on" / "notes.md"), "around": "Lease > Notice", "context": 0})))
    assert "[[lease]]" in part["text"] and part["shown"]
    mapped = json.loads(text_of(call(server, "explore_documents", {"path": str(docs / "on" / "notes.md")})))
    assert {os.path.basename(d["path"]) for d in mapped["documents"]} >= {"notes.md", "lease.txt"}
    assert is_error(call(server, "explore_documents", {}))
    result = call(server, "read_document", {"path": str(docs / "on" / "notes.md"), "around": "nowhere"})
    assert is_error(result) and "Could not find" in text_of(result)


def test_server_restarts_itself_when_the_mcp_handler_dies(app, tmp_path):
    """If the MCP library's handler stops, the app rebuilds it instead of failing every request."""
    import urllib.request

    server, *_ = app
    http = HttpServer(server, str(tmp_path / "home" / "connection.json"), port=0)
    http.start()
    assert http.wait_started()

    def post():
        with open(tmp_path / "home" / "connection.json") as f:
            conn = json.load(f)
        req = urllib.request.Request(conn["url"], data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                                     method="POST", headers={"Authorization": f"Bearer {conn['token']}",
                                                             "Content-Type": "application/json",
                                                             "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status

    try:
        assert post() == 200
        server._lowlevel_server._session_manager._task_group = None  # what the crash left behind
        with pytest.raises(Exception):
            post()
        end = time.time() + 15
        while http.restarts == 0 and time.time() < end:
            time.sleep(0.1)
        assert http.restarts == 1
        assert post() == 200
    finally:
        http.stop()


def test_unexpected_tool_error_does_not_escape(app, monkeypatch):
    server, settings, engine, *_ = app

    def boom(*a, **k):
        raise UnicodeEncodeError("charmap", "？", 0, 1, "character maps to <undefined>")
    monkeypatch.setattr(engine, "read_file", boom)
    result = call(server, "read_document", {"path": "x？.md"})
    assert is_error(result)
    assert not is_error(call(server, "list_document_collections", {}))


def test_odd_characters_in_logs_cannot_break_the_server(app, tmp_path, monkeypatch):
    """The 4:59 PM crash: a refused path with "？" logged to a cp1252 stderr killed the MCP handler."""
    import io
    import logging
    import urllib.request

    from collection_search.mcp_server import harden_logging

    server, *_ = app
    cp1252 = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")  # strict, like the Windows build's stderr
    monkeypatch.setattr(sys, "stderr", cp1252)
    handler = logging.StreamHandler()  # on sys.stderr, as the app adds it
    root = logging.getLogger()
    root.addHandler(handler)
    old_level, old_raise = root.level, logging.raiseExceptions
    root.setLevel(logging.INFO)  # as in the app
    harden_logging()
    http = HttpServer(server, str(tmp_path / "home" / "connection.json"), port=0)
    http.start()
    assert http.wait_started()

    def post(body):
        conn = json.loads((tmp_path / "home" / "connection.json").read_text())
        req = urllib.request.Request(conn["url"], data=body, method="POST",
                                     headers={"Authorization": f"Bearer {conn['token']}",
                                              "Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    try:
        bad = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "read_document", "arguments": {"path": "C:\\\\x\\\\Why Is That\uff1f.en.md"}}}).encode()
        assert post(bad)["result"]["isError"]
        assert "result" in post(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}')
        assert http.restarts == 0  # never broke in the first place
    finally:
        http.stop()
        root.removeHandler(handler)
        root.setLevel(old_level)
        logging.raiseExceptions = old_raise
