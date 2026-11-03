"""The search engine as the rest of the app sees it.

Owns the embedding model, the index and the background indexer thread. The
MCP server and the window call it; it reads the active collections from
settings on every call and passes them to the search core, which never
reads settings itself.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from . import search as search_core
from .embeddings import Embedder, FastEmbedder
from .indexer import sync
from .settings import INDEX, Settings
from .store import Store

log = logging.getLogger(__name__)

EmbedderFactory = Callable[[str], Embedder]


class NotReady(Exception):
    pass


@dataclass(frozen=True)
class Status:
    state: str = "starting"  # starting, loading model, indexing, idle, error, stopped
    message: str = "Starting"
    done: int = 0
    total: int = 0
    current: str = ""
    files: int = 0
    chunks: int = 0
    errors: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    last_sync: float | None = None


def default_embedder_factory(models_dir: str | None) -> EmbedderFactory:
    return lambda model: FastEmbedder(model, cache_dir=models_dir)


class Engine:
    def __init__(self, settings: Settings, db_path: str, embedder_factory: EmbedderFactory):
        self.settings = settings
        self.db_path = db_path
        self._factory = embedder_factory
        self._embedder: Embedder | None = None
        self._embed_lock = threading.Lock()  # one embedding call at a time across threads
        self._ready = threading.Event()
        self._local = threading.local()
        self._generation = 0  # bumped when the index is rebuilt, so thread-local stores reopen
        self._wake = threading.Event()
        self._sync_requested = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._watcher = None
        self._status = Status()
        self._status_lock = threading.Lock()
        self._listeners: list[Callable[[Status], None]] = []
        settings.subscribe(self._on_settings)

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="indexer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stopping.set()
        self._wake.set()
        if self._watcher:
            self._watcher.stop()
        if self._thread:
            self._thread.join(timeout)
        self._set_status(state="stopped", message="Stopped")

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def request_sync(self) -> None:
        self._sync_requested.set()
        self._wake.set()

    def reindex_all(self) -> None:
        """Rebuild the index from scratch (the Status tab's Reindex button)."""
        self._rebuild_requested = True
        self.request_sync()

    # ---- status ---------------------------------------------------------------
    def status(self) -> Status:
        with self._status_lock:
            return self._status

    def on_status(self, listener: Callable[[Status], None]) -> None:
        self._listeners.append(listener)

    def _set_status(self, **changes) -> None:
        with self._status_lock:
            self._status = replace(self._status, **changes)
            status = self._status
        for listener in list(self._listeners):
            try:
                listener(status)
            except Exception:
                log.exception("Status listener failed")

    # ---- what the tools call ---------------------------------------------------
    def search(self, query: str, limit: int) -> list[search_core.Result]:
        store, embedder = self._for_query()
        prefs = self.settings.preferences
        limit = max(1, min(limit, prefs.max_result_count))
        with self._embed_lock:
            return search_core.search(store, _Locked(embedder, self._embed_lock, held=True), query,
                                      self.settings.active_collections(), limit)

    def read_file(self, path: str) -> search_core.FileText:
        store, _ = self._for_query()
        return search_core.read_file(store, path, self.settings.active_collections())

    def active_collection_names(self) -> list[str]:
        return [c.name for c in self.settings.active_collections()]

    def _for_query(self) -> tuple[Store, Embedder]:
        if not self._ready.is_set():
            status = self.status()
            raise NotReady(
                f"Collection Search is not ready yet ({status.message.lower()}). Try again in a minute."
            )
        return self._store(), self._embedder

    def _store(self) -> Store:
        local = self._local
        if getattr(local, "generation", None) != self._generation:
            if getattr(local, "store", None):
                local.store.close()
            local.store = Store(self.db_path, self._embedder.name, self._embedder.dim)
            local.generation = self._generation
        return local.store

    # ---- settings changes -----------------------------------------------------
    def _on_settings(self, kind: str) -> None:
        if kind == INDEX:
            self.request_sync()

    # ---- indexer thread -------------------------------------------------------
    _rebuild_requested = False

    def _run(self) -> None:
        try:
            self._load_model()
        except Exception as exc:
            log.exception("Model failed to load")
            self._set_status(state="error", message=f"Could not load the search model: {exc}")
            return
        self._sync_requested.set()
        while not self._stopping.is_set():
            if self._sync_requested.is_set():
                self._sync_requested.clear()
                self._sync_once()
                self._update_watcher()
            rescan = max(1, self.settings.preferences.rescan_minutes) * 60
            self._wake.wait(rescan)
            if not self._wake.is_set():
                self._sync_requested.set()  # periodic scan catches anything the watcher missed
            self._wake.clear()

    def _load_model(self) -> None:
        model = self.settings.preferences.model
        self._set_status(state="loading model", message=f"Loading search model {model} (first run downloads it)")
        self._embedder = self._factory(model)
        self._loaded_model = model
        # Reopen every thread's connection against the (possibly reset) index.
        Store(self.db_path, self._embedder.name, self._embedder.dim, reset_if_model_changed=True).close()
        self._generation += 1
        self._ready.set()

    def _sync_once(self) -> None:
        prefs = self.settings.preferences
        if prefs.model != self._loaded_model:
            self._ready.clear()
            self._load_model()
        store = self._store()
        if self._rebuild_requested:
            self._rebuild_requested = False
            store.reset(self._embedder.name, self._embedder.dim)
        self._set_status(state="indexing", message="Checking folders", done=0, total=0, current="")

        def progress(done, total, path):
            self._set_status(state="indexing", message=f"Indexing {done} of {total}",
                             done=done, total=total, current=path)

        def cancelled():
            return self._stopping.is_set() or self._sync_requested.is_set()

        try:
            result = sync(store, _Locked(self._embedder, self._embed_lock), self.settings.collections(),
                          progress, prefs.chunk_size, prefs.chunk_overlap, cancelled)
        except Exception as exc:
            log.exception("Sync failed")
            self._set_status(state="error", message=f"Indexing failed: {exc}")
            return
        stats = store.stats()
        if result.cancelled:
            return  # another sync is queued, or we are quitting
        self._set_status(
            state="idle", message="Up to date", done=0, total=0, current="",
            files=stats["files"], chunks=stats["chunks"], errors=tuple(store.errors()),
            last_sync=time.time(),
        )

    def _update_watcher(self) -> None:
        if not self.settings.preferences.watch_folders:
            return
        folders = sorted({f for c in self.settings.collections() for f in c.folders if os.path.isdir(f)})
        if self._watcher is None:
            from .watcher import FolderWatcher

            self._watcher = FolderWatcher(self.request_sync)
        self._watcher.watch(folders)


class _Locked:
    """Wraps an embedder so calls from different threads take turns."""

    def __init__(self, embedder: Embedder, lock: threading.Lock, held: bool = False):
        self._e, self._lock, self._held = embedder, lock, held
        self.name, self.dim = embedder.name, embedder.dim

    def embed_documents(self, texts):
        if self._held:
            return self._e.embed_documents(texts)
        with self._lock:
            return self._e.embed_documents(texts)

    def embed_query(self, text):
        if self._held:
            return self._e.embed_query(text)
        with self._lock:
            return self._e.embed_query(text)
