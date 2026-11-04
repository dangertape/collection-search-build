"""Stdio shim: what Claude Desktop launches.

Claude Desktop starts MCP servers itself and talks to them over stdin and
stdout. The real server lives in the running app, so this small process
forwards each JSON-RPC message to it over localhost and writes the reply
back. It uses only the standard library so it starts fast.

Claude Desktop gives a connector only a short time to answer its first
messages, and marks it "couldn't connect" otherwise. So the shim never lets
those first messages wait on the app:
  - If the app isn't running, the shim starts it (in the tray) and waits for it.
  - `initialize` is answered from a saved copy of the app's last answer when
    the app is slow or still starting, and the tool list likewise.
  - Tool calls wait for the app to come up before giving up.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .paths import connection_path, is_frozen, program_dir, roaming_dir

NOT_RUNNING = ("Collection Search is not running and could not be started. "
               "Start the Collection Search app, then try again.")
STARTING = ("Collection Search is running but not answering yet (it may be starting or restarting "
            "its connection). Try again in a minute.")
TIMEOUT_SECONDS = 120  # a search can wait on the model loading
QUICK_SECONDS = 5  # initialize and tools/list: answer from the saved copy if the app is slower
APP_WAIT_SECONDS = 60  # how long a tool call waits for a starting app
APP_EXE = "CollectionSearch.exe"
LAUNCH_MARKER = "start-in-tray"  # tells an app started by the shim to stay in the tray
SERVER_NAME = "my-documents"
DEFAULT_INSTRUCTIONS = "Searches the user's own notes and documents stored on their computer."

_launched = False


def main() -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    for line in iter(stdin.readline, b""):
        line = line.strip()
        if not line:
            continue
        reply = forward(line)
        if reply:
            stdout.write(reply + b"\n")
            stdout.flush()
    return 0


def forward(message: bytes) -> bytes | None:
    """Answer one JSON-RPC message (or batch). Returns the reply line, if any."""
    try:
        parsed = json.loads(message)
    except ValueError:
        return _error(None, -32700, "Parse error")
    if isinstance(parsed, list):
        return _forward_plain(message, parsed)
    method = parsed.get("method") if isinstance(parsed, dict) else None
    request_id = parsed.get("id") if isinstance(parsed, dict) else None
    is_request = isinstance(parsed, dict) and "id" in parsed and method is not None

    if method in ("initialize", "tools/list"):
        try:
            reply = _send(message, QUICK_SECONDS)
            if reply:
                _remember(method, reply)
            return reply
        except _Unreachable:
            ensure_app_running()
            saved = _saved(method)
            if method == "initialize":
                return _answer(request_id, _initialize_result(parsed, saved))
            if saved is None:  # never seen the app's tools yet: wait for it once
                return _wait_and_send(message, request_id)
            return _answer(request_id, saved)
    if method == "ping" and is_request:
        return _answer(request_id, {})
    if not is_request:  # a notification such as notifications/initialized
        try:
            _send(message, QUICK_SECONDS)
        except _Unreachable:
            pass
        return None
    try:
        return _send(message, TIMEOUT_SECONDS)
    except _Unreachable:
        ensure_app_running()
        return _wait_and_send(message, request_id)


# ---- talking to the app ----------------------------------------------------------

class _Unreachable(Exception):
    """The app isn't running, isn't listening yet, or didn't answer in time."""


def _send(message: bytes, timeout: float) -> bytes | None:
    try:
        with open(connection_path(), encoding="utf-8") as f:
            conn = json.load(f)
        request = urllib.request.Request(
            conn["url"],
            data=message,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {conn['token']}",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.status == 202 or not body.strip():
                return None
            if response.headers.get_content_type() == "text/event-stream":
                return _from_sse(body)
            return body.strip()
    except urllib.error.HTTPError as exc:
        body = exc.read().strip()
        if body.startswith(b"{"):
            return body
        raise _Unreachable(str(exc)) from exc
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        raise _Unreachable(str(exc)) from exc


def _wait_and_send(message: bytes, request_id) -> bytes | None:
    end = time.monotonic() + APP_WAIT_SECONDS
    while True:
        try:
            return _send(message, TIMEOUT_SECONDS)
        except _Unreachable:
            if time.monotonic() >= end:
                text = STARTING if _app_pid_alive() else NOT_RUNNING
                return _error(request_id, -32000, text)
            time.sleep(1)


def _forward_plain(message: bytes, parsed: list) -> bytes | None:
    """Batches are rare: forward as they are, with an error per request if the app is away."""
    try:
        return _send(message, TIMEOUT_SECONDS)
    except _Unreachable:
        ids = _request_ids(parsed)
        if not ids:
            return None
        return json.dumps([json.loads(_error(i, -32000, NOT_RUNNING)) for i in ids]).encode()


# ---- starting the app --------------------------------------------------------------

def ensure_app_running() -> None:
    """Start the app in the tray if it isn't running. Once per shim, never twice at once."""
    global _launched
    if _launched or _app_pid_alive():
        return
    _launched = True
    try:
        with open(os.path.join(roaming_dir(), LAUNCH_MARKER), "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    try:
        if is_frozen():
            exe = os.path.join(program_dir(), APP_EXE)
            if os.name == "nt":
                # Through Explorer, so the app runs on its own and isn't closed
                # along with Claude Desktop (which owns this shim process).
                subprocess.Popen(["explorer.exe", exe], close_fds=True)
            else:
                subprocess.Popen([exe, "--tray"], start_new_session=True, close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        else:
            kwargs = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen([sys.executable, "-m", "collection_search", "--tray"], close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **kwargs)
    except OSError:
        pass


def _app_pid_alive() -> bool:
    try:
        with open(connection_path(), encoding="utf-8") as f:
            pid = int(json.load(f)["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return pid_alive(pid)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---- saved answers ------------------------------------------------------------------

def _cache_path() -> str:
    return os.path.join(roaming_dir(), "connector-cache.json")


def _remember(method: str, reply: bytes) -> None:
    try:
        result = json.loads(reply).get("result")
    except (ValueError, AttributeError):
        return
    if result is None:
        return
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if cache.get(method) == result:
        return
    cache[method] = result
    tmp = _cache_path() + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, _cache_path())
    except OSError:
        pass


def _saved(method: str):
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            return json.load(f).get(method)
    except (OSError, ValueError, AttributeError):
        return None


def _initialize_result(request: dict, saved: dict | None) -> dict:
    result = dict(saved) if saved else {
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": "0"},
        "instructions": DEFAULT_INSTRUCTIONS,
    }
    asked = (request.get("params") or {}).get("protocolVersion")
    result["protocolVersion"] = asked or result.get("protocolVersion") or "2025-06-18"
    return result


# ---- JSON-RPC helpers ----------------------------------------------------------------

def _request_ids(parsed) -> list:
    items = parsed if isinstance(parsed, list) else [parsed]
    return [m["id"] for m in items if isinstance(m, dict) and "id" in m and "method" in m]


def _from_sse(body: bytes) -> bytes | None:
    data = [line[5:].strip() for line in body.splitlines() if line.startswith(b"data:")]
    return b"\n".join(d for d in data if d) or None


def _answer(request_id, result) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()


def _error(request_id, code: int, message: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}).encode()


if __name__ == "__main__":
    raise SystemExit(main())
