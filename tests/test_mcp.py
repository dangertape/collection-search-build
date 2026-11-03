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


def test_tool_list_has_no_collection_parameter(app):
    server, *_ = app

    async def go():
        return await server.list_tools()
    tools = {t.name: t for t in anyio.run(go)}
    assert set(tools) == {"search", "read_file", "list_collections", "find_similar"}
    for t in tools.values():
        schema = t.input_schema if hasattr(t, "input_schema") else t.inputSchema
        assert not any("collection" in p for p in schema.get("properties", {}))


def test_search_returns_only_active(app):
    server, settings, engine, docs, off = app
    data = json.loads(text_of(call(server, "search", {"query": "zebracorn notes", "limit": 10})))
    assert all("secret" not in r["path"] for r in data["results"])
    settings.set_collection_active(off.id, True)
    data = json.loads(text_of(call(server, "search", {"query": "zebracorn notes", "limit": 10})))
    assert any("secret" in r["path"] for r in data["results"])


def test_list_collections(app):
    server, *_ = app
    assert json.loads(text_of(call(server, "list_collections", {}))) == {"active_collections": ["On"]}


def test_disabled_tool_errors_on_next_call(app):
    server, settings, *_ = app
    settings.set_tool_enabled("search", False)
    result = call(server, "search", {"query": "notice"})
    assert is_error(result) and disabled_message("search") in text_of(result)
    settings.set_tool_enabled("search", True)
    assert not is_error(call(server, "search", {"query": "notice"}))


def test_read_file_scope(app):
    server, settings, engine, docs, off = app
    ok = call(server, "read_file", {"path": str(docs / "on" / "lease.txt")})
    assert "notice period" in json.loads(text_of(ok))["text"]
    for path in [str(docs / "off" / "secret.txt"),
                 str(docs / "on" / ".." / "off" / "secret.txt"),
                 "/etc/passwd",
                 str(docs / "on" / "missing.txt")]:
        result = call(server, "read_file", {"path": path})
        assert is_error(result) and "not in an active collection" in text_of(result)


def test_read_file_through_symlink_outside_scope(app, tmp_path):
    server, settings, engine, docs, off = app
    link = docs / "on" / "link.txt"
    try:
        os.symlink(docs / "off" / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    result = call(server, "read_file", {"path": str(link)})
    assert is_error(result)


def test_limit_is_validated(app):
    server, *_ = app
    assert is_error(call(server, "search", {"query": "x", "limit": 1000}))


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
                    found = await session.call_tool("search", {"query": "4471-B", "limit": 3})
                    settings.set_tool_enabled("search", False)
                    blocked = await session.call_tool("search", {"query": "4471-B"})
                    return names, found, blocked

        names, found, blocked = anyio.run(go)
        assert names == ["find_similar", "list_collections", "read_file", "search"]
        assert "lease.txt" in found.content[0].text
        assert is_error(blocked) and "turned off" in blocked.content[0].text
    finally:
        http.stop()
    assert not os.path.exists(str(tmp_path / "home" / "connection.json"))


def test_shim_reports_app_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLECTION_SEARCH_HOME", str(tmp_path))
    reply = json.loads(shim.forward(b'{"jsonrpc":"2.0","id":7,"method":"tools/list"}'))
    assert reply["id"] == 7 and "not running" in reply["error"]["message"]
    assert shim.forward(b'{"jsonrpc":"2.0","method":"notifications/initialized"}') is None
    assert json.loads(shim.forward(b"not json"))["error"]["code"] == -32700


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
    data = json.loads(text_of(call(server, "find_similar", {"path": str(docs / "on" / "lease.txt")})))
    assert [os.path.basename(r["path"]) for r in data["results"]] == ["lease2.txt"]
    assert data["results"][0]["passages"] and data["count"] == 1
    result = call(server, "find_similar", {"path": str(docs / "off" / "secret.txt")})
    assert is_error(result) and "not in an active collection" in text_of(result)
    settings.set_tool_enabled("find_similar", False)
    result = call(server, "find_similar", {"path": str(docs / "on" / "lease.txt")})
    assert is_error(result) and disabled_message("find_similar") in text_of(result)
