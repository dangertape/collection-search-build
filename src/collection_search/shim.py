"""Stdio shim: what Claude Desktop launches.

Claude Desktop starts MCP servers itself and talks to them over stdin and
stdout. The real server lives in the running app, so this small process
forwards each JSON-RPC message to it over localhost and writes the reply
back. It uses only the standard library so it starts fast.

If the app is not running, requests get a clear error instead of a hang.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from .paths import connection_path

NOT_RUNNING = "Collection Search is not running. Start the Collection Search app, then try again."
TIMEOUT_SECONDS = 120  # first search can wait on the model loading


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
    """Send one JSON-RPC message (or batch) to the app. Returns the reply line, if any."""
    try:
        parsed = json.loads(message)
    except ValueError:
        return _error(None, -32700, "Parse error")
    ids = _request_ids(parsed)
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
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read()
            if response.status == 202 or not body.strip():
                return None
            if response.headers.get_content_type() == "text/event-stream":
                return _from_sse(body)
            return body.strip()
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            body = exc.read().strip()
            if body.startswith(b"{"):
                return body
        if not ids:
            return None  # a notification: nothing to answer
        errors = [json.loads(_error(i, -32000, NOT_RUNNING)) for i in ids]
        return json.dumps(errors if isinstance(parsed, list) else errors[0]).encode()


def _request_ids(parsed) -> list:
    items = parsed if isinstance(parsed, list) else [parsed]
    return [m["id"] for m in items if isinstance(m, dict) and "id" in m and "method" in m]


def _from_sse(body: bytes) -> bytes | None:
    data = [line[5:].strip() for line in body.splitlines() if line.startswith(b"data:")]
    return b"\n".join(d for d in data if d) or None


def _error(request_id, code: int, message: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}).encode()


if __name__ == "__main__":
    raise SystemExit(main())
