"""Keep the index in step with the collections' folders."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .chunking import DEFAULT_OVERLAP, DEFAULT_SIZE, chunk_sections
from .collections import Collection
from .embeddings import Embedder
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


def files_for(collections: Iterable[Collection]) -> dict[str, tuple[str, int, float]]:
    """Every file covered by any collection (active or not): key -> (path, size, mtime)."""
    collections = list(collections)
    found: dict[str, tuple[str, int, float]] = {}
    for col in collections:
        for root in col.folders:
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune excluded folders early so we don't walk them.
                dirnames[:] = [
                    d for d in dirnames
                    if not any(path_key(os.path.join(dirpath, d)) == path_key(ex) for ex in col.excluded)
                ]
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    key = path_key(path)
                    if key in found or not col.covers(key):
                        continue
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    found[key] = (os.path.abspath(path), st.st_size, st.st_mtime)
    return found


def sync(
    store: Store,
    embedder: Embedder,
    collections: Iterable[Collection],
    progress: ProgressFn | None = None,
    chunk_size: int = DEFAULT_SIZE,
    chunk_overlap: int = DEFAULT_OVERLAP,
    cancelled: Callable[[], bool] | None = None,
) -> SyncResult:
    """Index new and changed files, drop removed ones. Compares by path, size and mtime.

    `cancelled` is checked between files so Quit or a settings change can stop a long run.
    """
    wanted = files_for(collections)
    indexed = store.files()
    result = SyncResult()

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

    for done, (key, path, size, mtime, existed) in enumerate(todo):
        if cancelled and cancelled():
            result.cancelled = True
            break
        if progress:
            progress(done, len(todo), path)
        try:
            chunks = chunk_sections(parse(path), chunk_size, chunk_overlap)
            vectors = embedder.embed_documents([c.text for c in chunks]) if chunks else []
            error = None if chunks else "No text found (scanned or empty file?)"
        except ParseError as exc:
            chunks, vectors, error = [], [], str(exc)
        if error:
            result.errors.append((path, error))
        store.replace_file(path, size, mtime, chunks, vectors, error)
        if existed:
            result.updated += 1
        else:
            result.added += 1
    if progress and not result.cancelled:
        progress(len(todo), len(todo), "")
    return result
