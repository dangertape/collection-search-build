"""Where the app keeps its files.

Settings and the connection file live in %APPDATA%; the index and the
embedding model live in %LOCALAPPDATA% (machine-specific, can be large).
Set COLLECTION_SEARCH_HOME to keep everything in one folder instead, which
tests and development use.
"""

from __future__ import annotations

import os
import sys

APP_DIR_NAME = "CollectionSearch"


def _home_override() -> str | None:
    return os.environ.get("COLLECTION_SEARCH_HOME") or None


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def roaming_dir() -> str:
    base = _home_override() or os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return _ensure(base if _home_override() else os.path.join(base, APP_DIR_NAME))


def local_dir() -> str:
    override = _home_override()
    if override:
        return _ensure(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return _ensure(os.path.join(base, APP_DIR_NAME))


def settings_path() -> str:
    return os.path.join(roaming_dir(), "settings.json")


def connection_path() -> str:
    """Port and token of the running app, read by the stdio shim."""
    return os.path.join(roaming_dir(), "connection.json")


def lock_path() -> str:
    return os.path.join(roaming_dir(), "app.lock")


def index_path() -> str:
    return os.path.join(local_dir(), "index.db")


def models_dir() -> str:
    """A 'models' folder shipped next to the program wins, so a bundled model is used offline."""
    # PyInstaller 6 puts bundled data in _internal (sys._MEIPASS), not beside the .exe.
    for base in (getattr(sys, "_MEIPASS", None), program_dir()):
        bundled = os.path.join(base, "models") if base else ""
        if bundled and os.path.isdir(bundled):
            return bundled
    return _ensure(os.path.join(local_dir(), "models"))


def log_path() -> str:
    return os.path.join(local_dir(), "app.log")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def program_dir() -> str:
    """Folder holding the .exe when packaged, or the repo root in development."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def addon_dirs() -> list[str]:
    """Folders searched for user add-on tools, besides the built-in ones."""
    return [os.path.join(program_dir(), "tools"), os.path.join(roaming_dir(), "tools")]
