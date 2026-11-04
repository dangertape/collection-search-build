"""Watch collection folders and ask for a sync shortly after files change."""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 3.0  # saving a document fires several events; wait for them to settle


class FolderWatcher(FileSystemEventHandler):
    """Calls on_change a moment after a relevant change settles.

    `wanted(path)` says whether a file matters (an indexed type in a
    collection). Changes to other files, such as Obsidian's own settings in
    .obsidian or a sync app's temporary files, are ignored, so they can't
    keep starting new scans.
    """

    def __init__(self, on_change: Callable[[], None], wanted: Callable[[str], bool] | None = None):
        self._on_change = on_change
        self._wanted = wanted
        self._observer: Observer | None = None
        self._folders: list[str] = []
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def watch(self, folders: list[str]) -> None:
        if folders == self._folders and self._observer is not None:
            return
        self.stop()
        self._folders = folders
        if not folders:
            return
        observer = Observer()
        for folder in folders:
            try:
                observer.schedule(self, folder, recursive=True)
            except OSError as exc:
                log.warning("Cannot watch %s: %s", folder, exc)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        with self._lock:
            if self._timer:
                self._timer.cancel()

    def relevant(self, event) -> bool:
        if event.event_type in ("opened", "closed", "closed_no_write"):
            return False
        paths = [p for p in (event.src_path, getattr(event, "dest_path", "")) if p]
        paths = [os.fsdecode(p) for p in paths]
        if event.is_directory:
            # A folder appearing, going or moving can carry indexed files with it.
            return event.event_type != "modified" and not all(_hidden(p) for p in paths)
        if self._wanted is None:
            return True
        return any(self._wanted(p) for p in paths)

    def on_any_event(self, event) -> None:
        if not self.relevant(event):
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._on_change)
            self._timer.daemon = True
            self._timer.start()


def _hidden(path: str) -> bool:
    """Inside a folder like .obsidian, .git or a sync app's @eaDir / #recycle."""
    parts = os.path.normpath(path).split(os.sep)
    return any(p[:1] in (".", "@", "#", "~") for p in parts if p not in (".", ".."))
