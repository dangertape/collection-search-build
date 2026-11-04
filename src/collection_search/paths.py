r"""Where the app keeps its files.

On Windows everything lives in %USERPROFILE%\.collection-search, not in
AppData. Claude Desktop installs as a packaged (MSIX) app, and programs it
starts (the connector, or the app itself) see a private copy of AppData.
The app and its connector must see the same files: the connector reads the
app's connection file, and an index split between the two copies corrupts.

Elsewhere, settings and the connection file live in ~/.config; the index and
the embedding model in ~/.local/share.
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


WINDOWS_DIR_NAME = ".collection-search"


def _windows_dir() -> str | None:
    if os.name != "nt":
        return None
    return os.path.join(os.environ.get("USERPROFILE") or os.path.expanduser("~"), WINDOWS_DIR_NAME)


def roaming_dir() -> str:
    override = _home_override() or _windows_dir()
    if override:
        return _ensure(override)
    return _ensure(os.path.join(os.path.expanduser("~"), ".config", APP_DIR_NAME))


def local_dir() -> str:
    override = _home_override() or _windows_dir()
    if override:
        return _ensure(override)
    return _ensure(os.path.join(os.path.expanduser("~"), ".local", "share", APP_DIR_NAME))


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
