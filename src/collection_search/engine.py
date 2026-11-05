"""The search engine as the rest of the app sees it.

Owns the embedding model, the index and the background indexer thread. The
MCP server and the window call it; it reads the active collections from
settings on every call and passes them to the search core, which never
reads settings itself.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import sqlite3
import threading
import time
import weakref
from dataclasses import dataclass, field, replace
from typing import Callable

from . import search as search_core
from .embeddings import Embedder, FastEmbedder
from .indexer import sync
from .collections import Collection
from .settings import INDEX, Settings
from .store import Store

log = logging.getLogger(__name__)

EmbedderFactory = Callable[[str], Embedder]


class NotReady(Exception):
    pass


REBUILDING = "The search index was damaged and is being rebuilt. Try again in a minute."


def is_damaged(exc: BaseException) -> bool:
    """True for SQLite errors that mean the index file itself is corrupt."""
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.DatabaseError) and ("malformed" in msg or "not a database" in msg)


def set_aside(db_path: str) -> str:
    """Move a damaged index (and its -wal/-shm files) out of the way, keeping the latest copy."""
    target = db_path + ".damaged"
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.exists(src):
            os.replace(src, target + suffix)
    return target


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
        self._stores: weakref.WeakSet[Store] = weakref.WeakSet()  # every open connection
        self._damaged = threading.Event()  # set by any thread that finds the index corrupt
        self._wake = threading.Event()
        self._sync_requested = threading.Event()
        # Set when folders or rules change: the scan in progress is out of date and
        # stops early. A plain file change doesn't stop it; it only queues another scan.
        self._restart_requested = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._watcher = None
        self._status = Status()
        self._status_lock = threading.Lock()
        self._listeners: list[Callable[[Status], None]] = []
        self._map = None  # last similarity map, reused while nothing has changed
        self._map_lock = threading.Lock()
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
        for store in list(self._stores):  # release the index file
            with contextlib.suppress(Exception):
                store.close()
        self._set_status(state="stopped", message="Stopped")

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def request_sync(self) -> None:
        self._sync_requested.set()
        self._wake.set()

    def reindex_all(self) -> None:
        """Rebuild the index from scratch (the Status tab's Reindex button)."""
        self._rebuild_requested = True
        self._restart_requested.set()
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
    @contextlib.contextmanager
    def _guard(self):
        try:
            yield
        except sqlite3.DatabaseError as exc:
            if not is_damaged(exc):
                raise
            self._report_damage(exc)
            raise NotReady(REBUILDING) from exc

    def _report_damage(self, exc: BaseException) -> None:
        if not self._damaged.is_set():
            log.error("Index is damaged: %s", exc)
            self._damaged.set()
            self._ready.clear()
            self.request_sync()

    def search(self, query: str, limit: int) -> list[search_core.Result]:
        return self.search_page(query, limit)[0]

    def search_page(self, query: str, limit: int, offset: int = 0,
                    collections: list[Collection] | None = None) -> tuple[list[search_core.Result], bool]:
        """`collections` limits the search to those; None means the active collections."""
        with self._guard():
            return self._search(query, limit, offset, collections)

    def read_file(self, path: str, around: str | None = None, context: int = 4) -> search_core.FileText:
        with self._guard():
            store, _ = self._for_query()
            return search_core.read_file(store, path, self.settings.active_collections(),
                                         around=around, context=context)

    def document_links(self, path: str, limit: int = 30, collections: list[Collection] | None = None) -> dict:
        with self._guard():
            store, _ = self._for_query()
            return search_core.document_links(store, path, self._scope(collections), limit)

    def explore(self, query: str | None, path: str | None, steps: int = 2, max_documents: int = 15,
                collections: list[Collection] | None = None) -> dict:
        with self._guard():
            store, embedder = self._for_query()
            with self._embed_lock:
                return search_core.explore(store, _Locked(embedder, self._embed_lock, held=True),
                                           self._scope(collections), query, path, steps, max_documents)

    def find_similar(self, path: str, limit: int, passages: int = 2,
                     collections: list[Collection] | None = None) -> search_core.SimilarResult:
        with self._guard():
            store, _ = self._for_query()
            limit = max(1, min(limit, self.settings.preferences.max_result_count))
            return search_core.find_similar(store, path, self._scope(collections), limit, passages)

    def similarity_map(self, cancelled: Callable[[], bool] = lambda: False):
        """Map of the documents in the ticked collections (see similarity_map.py).

        The last map is kept and returned again until the index or the ticked collections change.
        """
        from .similarity_map import build_map

        with self._guard():
            store, _ = self._for_query()
            active = self.settings.active_collections()
            key = (repr(active), store.index_fingerprint())
            with self._map_lock:
                cached = self._map
            if cached is not None and cached.key == key:
                return cached
            built = build_map(store, active, key, cancelled)
            with self._map_lock:
                self._map = built
            return built

    def _search(self, query: str, limit: int, offset: int = 0,
                collections: list[Collection] | None = None) -> tuple[list[search_core.Result], bool]:
        store, embedder = self._for_query()
        prefs = self.settings.preferences
        limit = max(1, min(limit, prefs.max_result_count))
        with self._embed_lock:
            return search_core.search_page(store, _Locked(embedder, self._embed_lock, held=True), query,
                                           self._scope(collections), limit, offset=offset)

    def _scope(self, collections: list[Collection] | None) -> list[Collection]:
        """The collections a call searches. An explicit list is searched even if a collection is
        unticked, because the window lets the user search any collection. Anything serving Claude
        must pass only ticked collections, or None."""
        if collections is None:
            return self.settings.active_collections()
        return [c if c.active else replace(c, active=True) for c in collections]

    def active_collection_names(self) -> list[str]:
        return [c.name for c in self.settings.active_collections()]

    def _for_query(self) -> tuple[Store, Embedder]:
        if self._damaged.is_set():
            raise NotReady(REBUILDING)
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
            local.store = None
            local.store = Store(self.db_path, self._embedder.name, self._embedder.dim)
            self._stores.add(local.store)
            local.generation = self._generation
        return local.store

    # ---- settings changes -----------------------------------------------------
    def _on_settings(self, kind: str) -> None:
        if kind == INDEX:
            self._restart_requested.set()
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
                self._update_watcher()  # watch first, so a change during the scan isn't missed
                self._sync_once()
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
        self._open_index()

    def _open_index(self) -> None:
        """Check the index opens cleanly; a corrupt one is set aside and started fresh."""
        try:
            store = Store(self.db_path, self._embedder.name, self._embedder.dim, reset_if_model_changed=True)
            try:
                if store.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("database disk image is malformed (quick_check)")
            finally:
                store.close()
        except sqlite3.DatabaseError as exc:
            if not is_damaged(exc):
                raise
            damage = str(exc)
        else:
            damage = None
        if damage:
            self._repair(damage)
            return
        # Reopen every thread's connection against the (possibly reset) index.
        self._generation += 1
        self._damaged.clear()
        self._ready.set()

    def _repair(self, reason: str) -> None:
        log.error("Index is damaged (%s); setting it aside and rebuilding", reason)
        self._ready.clear()
        self._damaged.set()
        self._set_status(state="indexing", message="The index was damaged. Rebuilding it", done=0, total=0)
        self._generation += 1
        for store in list(self._stores):  # release the file so it can be moved
            with contextlib.suppress(Exception):
                store.close()
        local = self._local
        local.store = None
        local.generation = None
        gc.collect()  # finish closing connections whose last statements were just released
        moved = set_aside(self.db_path)
        log.info("Damaged index moved to %s", moved)
        Store(self.db_path, self._embedder.name, self._embedder.dim).close()
        self._generation += 1
        self._damaged.clear()
        self._ready.set()

    def _sync_once(self) -> None:
        damage = None
        try:
            self._sync_inner()
        except sqlite3.DatabaseError as exc:
            if not is_damaged(exc):
                log.exception("Sync failed")
                self._set_status(state="error", message=f"Indexing failed: {exc}")
                return
            damage = str(exc)
        except Exception as exc:  # keep the indexer thread alive so the buttons still work
            log.exception("Sync failed")
            self._set_status(state="error", message=f"Indexing failed: {exc}")
            return
        if damage:
            try:
                self._repair(damage)
                self._sync_inner()
            except Exception as exc:
                log.exception("Could not rebuild the damaged index")
                self._set_status(state="error", message=f"Could not rebuild the damaged index: {exc}")

    def _sync_inner(self) -> None:
        prefs = self.settings.preferences
        if prefs.model != self._loaded_model:
            self._ready.clear()
            self._load_model()
        if self._damaged.is_set():
            self._repair("reported by a search")
        store = self._store()
        if self._rebuild_requested:
            self._rebuild_requested = False
            store.reset(self._embedder.name, self._embedder.dim)
        self._restart_requested.clear()
        self._set_status(state="indexing", message="Checking folders", done=0, total=0, current="")

        def scanning(found):
            self._set_status(state="indexing", message=f"Checking folders ({found:,} files so far)")

        def progress(done, total, path):
            self._set_status(state="indexing", message=f"Indexing {done} of {total}",
                             done=done, total=total, current=path)

        def cancelled():
            return self._stopping.is_set() or self._restart_requested.is_set()

        result = sync(store, _Locked(self._embedder, self._embed_lock), self.settings.collections(),
                      progress, prefs.chunk_size, prefs.chunk_overlap, cancelled, scanning)
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

            self._watcher = FolderWatcher(self.request_sync, self._wanted)
        self._watcher.watch(folders)


    def _wanted(self, path: str) -> bool:
        """Does a changed file matter: an indexed type inside a collection's folders?"""
        from .store import path_key

        ext = os.path.splitext(path)[1].lower()
        collections = self.settings.collections()
        if not any(ext in c._exts for c in collections):
            return False
        try:
            key = path_key(path)
        except (OSError, ValueError):
            return True
        return any(c.covers(key) for c in collections)


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
