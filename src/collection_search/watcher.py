"""Watch collection folders and ask for a sync shortly after files change."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 3.0  # saving a document fires several events; wait for them to settle


class FolderWatcher(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
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

    def on_any_event(self, event) -> None:
        if event.is_directory and event.event_type == "modified":
            return
        with self._lock:
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._on_change)
            self._timer.daemon = True
            self._timer.start()
