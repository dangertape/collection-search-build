"""Search scoped to active collections.

The active collections are always passed in; this module never reads app
state. The allowed file set is worked out first and handed to the store, so
nothing outside it is ranked or returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .collections import Collection
from .embeddings import Embedder
from .store import ChunkHit, Store

RRF_K = 60  # standard reciprocal-rank-fusion constant
CANDIDATES = 50  # per method, before fusion


@dataclass(frozen=True)
class Result:
    text: str
    path: str
    collections: tuple[str, ...]
    location: str
    score: float


def allowed_files(store: Store, active: Iterable[Collection]) -> dict[int, tuple[str, ...]]:
    """file id -> names of the active collections that allow it."""
    active = [c for c in active if c.active]
    allowed: dict[int, tuple[str, ...]] = {}
    if not active:
        return allowed
    for key, rec in store.files().items():
        names = tuple(c.name for c in active if c.allows(key, rec.mtime))
        if names:
            allowed[rec.id] = names
    return allowed


def search(
    store: Store,
    embedder: Embedder,
    query: str,
    active: Iterable[Collection],
    limit: int = 8,
    use_keywords: bool = True,
) -> list[Result]:
    query = query.strip()
    if not query or limit <= 0:
        return []
    allowed = allowed_files(store, active)
    if not allowed:
        return []
    ids = list(allowed)
    semantic = store.vector_search(embedder.embed_query(query), ids, CANDIDATES)
    keyword = store.keyword_search(query, ids, CANDIDATES) if use_keywords else []
    fused = _fuse([semantic, keyword])[:limit]

    file_of = _file_ids(store, [h.chunk_id for h, _ in fused])
    return [
        Result(hit.text, hit.path, allowed[file_of[hit.chunk_id]], hit.location, round(score, 5))
        for hit, score in fused
    ]


def _fuse(rankings: list[list[ChunkHit]]) -> list[tuple[ChunkHit, float]]:
    scores: dict[int, float] = {}
    hits: dict[int, ChunkHit] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            hits.setdefault(hit.chunk_id, hit)
    order = sorted(scores, key=scores.get, reverse=True)
    return [(hits[cid], scores[cid]) for cid in order]


def _file_ids(store: Store, chunk_ids: list[int]) -> dict[int, int]:
    if not chunk_ids:
        return {}
    marks = ",".join("?" * len(chunk_ids))
    rows = store.db.execute(f"SELECT id, file_id FROM chunks WHERE id IN ({marks})", chunk_ids).fetchall()
    return dict(rows)
