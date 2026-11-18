"""Keep the index in step with the collections' folders."""

from __future__ import annotations

import os
import stat
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .chunking import DEFAULT_OVERLAP, DEFAULT_SIZE, chunk_sections
from .collections import Collection
from .embeddings import Embedder
from .links import extract as extract_links
from .parsers import ParseError, parse
from .store import Store, path_key

ProgressFn = Callable[[int, int, str], None]  # done, total, current path


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False


ScanFn = Callable[[int], None]  # files found so far while walking the folders

PARSE_WORKERS = 2  # files read in the background while the model embeds the previous ones
LOOKAHEAD = 16  # files read ahead of the one being embedded
DEFAULT_BATCH = 64  # passages per model call when the embedder doesn't say
SCAN_REPORT_EVERY = 500


def files_for(
    collections: Iterable[Collection],
    known: Iterable[str] = (),
    scanning: ScanFn | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, tuple[str, int, float]]:
    """Every file covered by any collection (active or not): key -> (path, size, mtime).

    Keys are built from each folder's normalised path plus the names found
    under it, instead of resolving every file on disk, which is slow on
    Windows and on synced drives. A symlinked file, or a key the index
    doesn't know, is checked the slow way so it matches what is stored.
    """
    collections = list(collections)
    known = set(known)
    found: dict[str, tuple[str, int, float]] = {}
    for col in collections:
        excluded = {path_key(ex) for ex in col.excluded}
        for root in col.folders:
            root_key = path_key(root)
            for dirpath, dirnames, filenames in os.walk(root):
                if cancelled and cancelled():
                    return found
                rel = os.path.relpath(dirpath, root)
                dir_key = root_key if rel == "." else os.path.normcase(os.path.join(root_key, rel))
                # Prune excluded folders early so we don't walk them.
                dirnames[:] = [d for d in dirnames if os.path.normcase(os.path.join(dir_key, d)) not in excluded]
                for name in filenames:
                    key = os.path.normcase(os.path.join(dir_key, name))
                    if os.path.splitext(key)[1] not in col._exts or key in found:
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        st = os.lstat(path)
                        if stat.S_ISLNK(st.st_mode):
                            st = os.stat(path)
                            key = path_key(path)
                        elif key not in known:
                            key = path_key(path)
                    except OSError:
                        continue
                    if key in found or not col.covers(key):
                        continue
                    found[key] = (os.path.abspath(path), st.st_size, st.st_mtime)
                    if scanning and len(found) % SCAN_REPORT_EVERY == 0:
                        scanning(len(found))
    return found


def sync(
    store: Store,
    embedder: Embedder,
    collections: Iterable[Collection],
    progress: ProgressFn | None = None,
    chunk_size: int = DEFAULT_SIZE,
    chunk_overlap: int = DEFAULT_OVERLAP,
    cancelled: Callable[[], bool] | None = None,
    scanning: ScanFn | None = None,
) -> SyncResult:
    """Index new and changed files, drop removed ones. Compares by path, size and mtime.

    `cancelled` is checked between files so Quit or a settings change can stop a long run.
    """
    indexed = store.files()
    result = SyncResult()
    wanted = files_for(collections, indexed.keys(), scanning, cancelled)
    if cancelled and cancelled():
        result.cancelled = True  # a partial walk must not remove the files it didn't reach
        return result

    removed = [key for key in indexed if key not in wanted]
    store.remove_files(removed)
    result.removed = len(removed)

    todo = []
    for key, (path, size, mtime) in wanted.items():
        old = indexed.get(key)
        if old and old.size == size and old.mtime == mtime:
            result.unchanged += 1
        else:
            todo.append((key, path, size, mtime, old is not None))
    # Newest first, so recent documents become searchable soonest.
    todo.sort(key=lambda t: t[3], reverse=True)

    # Files are read on worker threads while the model embeds earlier ones, and
    # passages from several small files share one model call, which is what keeps
    # a graphics card busy. Files are still stored in order, newest first.
    batch_target = max(1, getattr(embedder, "preferred_batch", DEFAULT_BATCH))
    batch: list[tuple[tuple, list, list, str | None]] = []
    batch_chunks = 0

    def flush() -> None:
        nonlocal batch, batch_chunks
        texts = [c.text for _, chunks, _, _ in batch for c in chunks]
        vectors = embedder.embed_documents(texts) if texts else []
        at = 0
        for (key, path, size, mtime, existed), chunks, links, error in batch:
            file_vectors = vectors[at:at + len(chunks)]
            at += len(chunks)
            if error:
                result.errors.append((path, error))
            store.replace_file(path, size, mtime, chunks, file_vectors, error, links)
            if existed:
                result.updated += 1
            else:
                result.added += 1
        batch, batch_chunks = [], 0

    pool = ThreadPoolExecutor(max_workers=PARSE_WORKERS, thread_name_prefix="read-file")
    waiting: deque = deque()
    upcoming = iter(todo)

    def read_ahead() -> None:
        while len(waiting) < LOOKAHEAD:
            item = next(upcoming, None)
            if item is None:
                return
            waiting.append((item, pool.submit(_read, item[1], chunk_size, chunk_overlap)))

    try:
        read_ahead()
        done = 0
        while waiting:
            if cancelled and cancelled():
                result.cancelled = True  # files read but not yet stored are picked up next time
                break
            item, future = waiting.popleft()
            read_ahead()
            if progress:
                progress(done, len(todo), item[1])
            chunks, links, error = future.result()
            batch.append((item, chunks, links, error))
            batch_chunks += len(chunks)
            done += 1
            if batch_chunks >= batch_target:
                flush()
        if not result.cancelled:
            flush()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if progress and not result.cancelled:
        progress(len(todo), len(todo), "")
    return result


def _read(path: str, chunk_size: int, chunk_overlap: int) -> tuple[list, list, str | None]:
    """Parse and chunk one file: (chunks, links, error). Runs on a worker thread."""
    try:
        sections = parse(path)
        chunks = chunk_sections(sections, chunk_size, chunk_overlap)
        links = extract_links(path, sections)
        error = None if chunks else "No text found (scanned or empty file?)"
    except ParseError as exc:
        chunks, links, error = [], [], str(exc)
    return chunks, links, error
