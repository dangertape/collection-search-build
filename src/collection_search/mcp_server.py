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
from mcp.types import ToolAnnotations

from . import __version__
from .addons import ToolContext, ToolSpec, bind
from .engine import NotReady
from .parsers import ParseError
from .search import LocationNotFound, NotAllowed, NothingToCompare

log = logging.getLogger(__name__)

DEFAULT_PORT = 47821
SERVER_NAME = "my-documents"
INSTRUCTIONS = (
    "Searches the user's own notes and documents stored on their computer (Markdown, Word, PDF and "
    "text files), through the Collection Search app. Use these tools when the user mentions their "
    "notes, documents, files, study materials or collections; they are not for images or other media. "
    "Only collections the user has switched on are visible; list_document_collections shows them. "
    "When the user asks about one collection by name, pass it as the collection argument to search only there. "
    "Be thorough: run several searches with different wording, ask for 20 or more results on research "
    "questions, follow links and backlinks with get_document_links or explore_documents, and read "
    "passages in context with read_document before answering. Every tool only reads; none change files."
)


def annotations(spec: ToolSpec) -> ToolAnnotations:
    """Tell Claude every tool only reads local files, so the app can treat them as safe."""
    title = spec.title or spec.name.replace("_", " ").capitalize()
    return ToolAnnotations(title=title, readOnlyHint=True, destructiveHint=False,
                           idempotentHint=True, openWorldHint=False)


def disabled_message(name: str) -> str:
    return f"The {name} tool is turned off in Collection Search. The user can turn it on in the app's Tools tab."


def build_server(engine, specs: list[ToolSpec]) -> MCPServer:
    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)
    ctx = ToolContext(engine)

    def gate(name: str) -> None:
        if not engine.settings.tool_enabled(name):
            raise ToolError(disabled_message(name))

    for spec in specs:
        server.add_tool(_friendly_errors(bind(spec, ctx, gate)), name=spec.name, title=spec.title or None,
                        description=spec.description, annotations=annotations(spec))
    return server


def _friendly_errors(fn):
    """Turn failures into short messages Claude can relay.

    Nothing a tool does may escape as an exception: an unexpected error that
    reaches the MCP library can take down its whole request handler, after
    which every request fails until the app restarts.
    """
    import functools

    @functools.wraps(fn)
    def wrapper(**kwargs):
        try:
            return fn(**kwargs)
        except ToolError:
            raise
        except (NotReady, NotAllowed, NothingToCompare, LocationNotFound, ParseError, ValueError) as exc:
            raise ToolError(_safe_text(str(exc))) from None
        except Exception as exc:
            log.exception("Tool %s failed", fn.__name__)
            raise ToolError(f"Collection Search hit an error: {_safe_text(type(exc).__name__)}. "
                            "Try again, or try another file.") from None

    return wrapper


def harden_logging() -> None:
    """A log line must never break a request.

    On Windows the app's stderr can use a code page that can't write "？" or
    emoji, which file names are full of. A failing log write then raised out of
    the MCP library's own logging call and stopped its request handler. Make
    the streams replace what they can't write, and make logging swallow its
    own errors.
    """
    import sys

    logging.raiseExceptions = False
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="backslashreplace")
            except (OSError, ValueError, AttributeError):
                pass


def _safe_text(text: str) -> str:
    """Text that survives any console or log encoding."""
    return text.encode("utf-8", "replace").decode("utf-8")


class _TokenCheck:
    """ASGI middleware: every request must carry the app's bearer token.

    It also notices when the MCP library's request handler has died (it then
    answers every request with "Task group is not initialized") and asks the
    server to restart itself, so Claude reconnects on its next try.
    """

    def __init__(self, app, token: str, on_broken=None):
        self.app, self.expected = app, f"Bearer {token}".encode()
        self.on_broken = on_broken

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not secrets.compare_digest(headers.get(b"authorization", b""), self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
            try:
                await self.app(scope, receive, send)
            except RuntimeError as exc:
                if "Task group is not initialized" in str(exc) and self.on_broken:
                    log.error("MCP request handler stopped; restarting the server for Claude")
                    self.on_broken()
                raise
            return
        await self.app(scope, receive, send)


class HttpServer:
    """Runs the MCP server on its own thread with its own event loop.

    If the MCP library's handler dies, the server is rebuilt and restarted on
    the same port and token, so Claude's connector keeps working.
    """

    def __init__(self, server: MCPServer, connection_file: str, port: int = DEFAULT_PORT):
        self.token = secrets.token_urlsafe(32)
        self.connection_file = connection_file
        self._server = server
        self._sock = _bind(port)
        self.port = self._sock.getsockname()[1]
        self._uvicorn = self._make_uvicorn()
        self._thread: threading.Thread | None = None
        self._restart_lock = threading.Lock()
        self._restarting = False
        self._stopped = False
        self.restarts = 0

    def _make_uvicorn(self) -> "uvicorn.Server":
        app = self._server.streamable_http_app(json_response=True, stateless_http=True, host="127.0.0.1")
        config = uvicorn.Config(_TokenCheck(app, self.token, self._broken), log_level="warning", lifespan="on",
            # uvicorn's own console logging crashes in the windowed .exe (no
            # stdout); its loggers go through the app's log file instead.
            log_config=None,
        )
        return uvicorn.Server(config)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> None:
        self._run_thread()
        self._write_connection_file()

    def _run_thread(self) -> None:
        self._thread = threading.Thread(target=self._uvicorn.run, kwargs={"sockets": [self._sock]},
                                        name="mcp", daemon=True)
        self._thread.start()

    def wait_started(self, timeout: float = 10) -> bool:
        import time

        end = time.time() + timeout
        while time.time() < end:
            if self._uvicorn.started:
                return True
            time.sleep(0.05)
        return False

    def _broken(self) -> None:
        with self._restart_lock:
            if self._restarting or self._stopped:
                return
            self._restarting = True
        threading.Thread(target=self._restart, name="mcp-restart", daemon=True).start()

    def _restart(self) -> None:
        try:
            self._uvicorn.should_exit = True
            if self._thread:
                self._thread.join(10)
            if self._stopped:
                return
            self._sock = _bind(self.port)
            self.port = self._sock.getsockname()[1]
            self._uvicorn = self._make_uvicorn()
            self._run_thread()
            self.wait_started()
            self._write_connection_file()
            self.restarts += 1
            log.info("MCP server restarted at %s", self.url)
        except Exception:
            log.exception("Could not restart the MCP server")
        finally:
            with self._restart_lock:
                self._restarting = False

    def stop(self) -> None:
        self._stopped = True
        self._uvicorn.should_exit = True
        if self._thread:
            self._thread.join(5)
        # Read and close the file before removing it: Windows can't delete an
        # open file, and a leftover file made the connector think the app was running.
        try:
            with open(self.connection_file, encoding="utf-8") as f:
                ours = json.load(f).get("token") == self.token
        except (OSError, ValueError):
            return
        if ours:
            try:
                os.remove(self.connection_file)
            except OSError:
                log.warning("Could not remove %s", self.connection_file)

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
