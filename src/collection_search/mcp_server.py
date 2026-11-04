"""The MCP server: exposes the tools to Claude and applies tool gating.

Claude always sees every registered tool. Whether a tool may run is checked
at call time against the app's switches, so turning a tool off applies on
the very next call in the same chat.

The server runs inside the app on 127.0.0.1 over streamable HTTP and requires
a random token. Claude Desktop reaches it through the stdio shim (shim.py).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import threading

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .addons import ToolContext, ToolSpec, bind
from .engine import NotReady
from .parsers import ParseError
from .search import NotAllowed, NothingToCompare

log = logging.getLogger(__name__)

DEFAULT_PORT = 47821
SERVER_NAME = "collection-search"
INSTRUCTIONS = (
    "Searches the user's local documents. Only collections the user has switched on in the "
    "Collection Search app are visible; call list_collections to see them."
)


def disabled_message(name: str) -> str:
    return f"The {name} tool is turned off in Collection Search. The user can turn it on in the app's Tools tab."


def build_server(engine, specs: list[ToolSpec]) -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)
    ctx = ToolContext(engine)

    def gate(name: str) -> None:
        if not engine.settings.tool_enabled(name):
            raise ToolError(disabled_message(name))

    for spec in specs:
        server.add_tool(_friendly_errors(bind(spec, ctx, gate)), name=spec.name, description=spec.description)
    return server


def _friendly_errors(fn):
    """Turn expected failures into short messages Claude can relay."""
    import functools

    @functools.wraps(fn)
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except (NotReady, NotAllowed, NothingToCompare, ParseError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


class _TokenCheck:
    """ASGI middleware: every request must carry the app's bearer token."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not secrets.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await self.app(scope, receive, send)


class HttpServer:
    """Runs the MCP server on its own thread with its own event loop."""

    def __init__(self, server: MCPServer, connection_file: str, port: int = DEFAULT_PORT):
        self.token = secrets.token_urlsafe(32)
        self.connection_file = connection_file
        self._sock = _bind(port)
        self.port = self._sock.getsockname()[1]
        app = server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")
        config = uvicorn.Config(_TokenCheck(app, self.token), log_level="warning", lifespan="on",
            # uvicorn's own console logging crashes in the windowed .exe (no
            # stdout); its loggers go through the app's log file instead.
            log_config=None,
        )
        self._uvicorn = uvicorn.Server(config)
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._uvicorn.run, kwargs={"sockets": [self._sock]},
                                        name="mcp", daemon=True)
        self._thread.start()
        self._write_connection_file()

    def wait_started(self, timeout: float = 10) -> bool:
        import time

        end = time.time() + timeout
        while time.time() < end:
            if self._uvicorn.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> None:
        self._uvicorn.should_exit = True
        if self._thread:
            self._thread.join(5)
        try:
            with open(self.connection_file, encoding="utf-8") as f:
                if json.load(f).get("token") == self.token:
                    os.remove(self.connection_file)
        except (OSError, ValueError):
            pass

    def _write_connection_file(self) -> None:
        tmp = self.connection_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"url": self.url, "token": self.token, "pid": os.getpid()}, f)
        os.replace(tmp, self.connection_file)


def _bind(port: int) -> socket.socket:
    """Prefer the fixed port; fall back to any free one (the shim reads the file, not the port)."""
    for candidate in (port, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", candidate))
            sock.listen(64)
            return sock
        except OSError:
            sock.close()
    raise OSError("No free port for the MCP server")
