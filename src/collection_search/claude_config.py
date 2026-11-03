"""Connect Claude Desktop to the app by adding the shim to its config file."""

from __future__ import annotations

import json
import os
import shutil
import sys

from .paths import is_frozen, program_dir

SERVER_KEY = "collection-search"
SHIM_EXE = "collection-search-mcp.exe"


def shim_command() -> dict:
    """The command Claude Desktop should run to reach the app."""
    if is_frozen():
        return {"command": os.path.join(program_dir(), SHIM_EXE)}
    return {"command": sys.executable, "args": ["-m", "collection_search", "--mcp"]}


def config_snippet() -> str:
    return json.dumps({"mcpServers": {SERVER_KEY: shim_command()}}, indent=2)


def claude_config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Claude", "claude_desktop_config.json")


def is_installed(path: str | None = None) -> bool:
    path = path or claude_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("mcpServers", {}).get(SERVER_KEY) == shim_command()
    except (OSError, ValueError, AttributeError):
        return False


def install(path: str | None = None) -> str:
    """Add or update our entry, keeping everything else. Backs up the old file first."""
    path = path or claude_config_path()
    data: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text) if text.strip() else {}
        shutil.copyfile(path, path + ".bak")
    if not isinstance(data, dict):
        raise ValueError("Claude's config file is not a JSON object; edit it by hand.")
    data.setdefault("mcpServers", {})[SERVER_KEY] = shim_command()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path
