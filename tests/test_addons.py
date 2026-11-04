import json
import textwrap

import anyio

from collection_search.addons import load_tools
from collection_search.mcp_server import build_server


class FakeEngine:
    class settings:
        preferences = type("P", (), {"default_result_count": 8})()

        @staticmethod
        def tool_enabled(name):
            return True

    def search(self, query, limit):
        return []

    def active_collection_names(self):
        return ["Client work"]


def test_builtin_tools_load():
    assert {t.name for t in load_tools()} == {"search", "read_file", "list_collections", "find_similar"}


def test_addon_file_becomes_a_tool(tmp_path):
    (tmp_path / "count_scope.py").write_text(textwrap.dedent('''
        from __future__ import annotations
        from typing import Annotated
        from pydantic import Field

        NAME = "count_scope"
        DESCRIPTION = "Count active collections."

        def handler(ctx, prefix: Annotated[str, Field(description="Text before the count")] = "n=") -> str:
            return prefix + str(len(ctx.active_collections()))
    '''))
    (tmp_path / "broken.py").write_text("raise RuntimeError('boom')")
    (tmp_path / "not_a_tool.py").write_text("X = 1")
    (tmp_path / "search.py").write_text("NAME='search'\nDESCRIPTION='dup'\ndef handler(ctx): return 1")
    tools = load_tools([str(tmp_path), str(tmp_path / "missing")])
    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"search", "read_file", "list_collections", "find_similar", "count_scope"}
    assert by_name["search"].source == "built-in"  # built-ins win name clashes

    server = build_server(FakeEngine(), tools)

    async def go():
        listed = {t.name: t for t in await server.list_tools()}
        result = await server.call_tool("count_scope", {"prefix": "active: "})
        return listed, result

    listed, result = anyio.run(go)
    schema = getattr(listed["count_scope"], "input_schema", None) or listed["count_scope"].inputSchema
    assert schema["properties"]["prefix"]["description"] == "Text before the count"
    assert result.content[0].text == "active: 1"
