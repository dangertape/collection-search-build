"""Search scoped to active collections.

The active collections are always passed in; this module never reads app
state. The allowed file set is worked out first and handed to the store, so
nothing outside it is ranked or returned.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from .collections import Collection
from .embeddings import Embedder
from .parsers import ParseError, parse
from .store import ChunkHit, FileRecord, Store, path_key

READ_LIMIT = 200_000  # characters returned by read_file


class NotAllowed(Exception):
    """The file is not in an active collection. Same message whether or not it exists."""

    def __init__(self, path: str):
        super().__init__(f"{path} is not in an active collection.")


@dataclass(frozen=True)
class FileText:
    path: str
    collections: tuple[str, ...]
    text: str
    truncated: bool

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


def _allowed_record(store: Store, path: str, active: Iterable[Collection]) -> tuple[FileRecord, tuple[str, ...]]:
    """The indexed file at `path` and the active collections allowing it, or NotAllowed.

    The path is normalised (absolute, symlinks resolved, case-folded on Windows)
    before any check, and the file is checked again on disk.
    """
    active = [c for c in active if c.active]
    key = path_key(path)
    record = store.file(key)
    if record is None:
        raise NotAllowed(path)
    try:
        mtime = os.stat(record.path).st_mtime
    except OSError:
        raise NotAllowed(path) from None
    names = tuple(c.name for c in active if c.allows(key, record.mtime) and c.allows(key, mtime))
    if not names:
        raise NotAllowed(path)
    return record, names


def read_file(store: Store, path: str, active: Iterable[Collection], limit: int = READ_LIMIT) -> FileText:
    """Full text of one indexed file, only if an active collection allows it."""
    record, names = _allowed_record(store, path, active)
    try:
        sections = parse(record.path)
    except ParseError as exc:
        raise ParseError(f"Could not read {record.path}: {exc}") from exc
    text = "\n\n".join(f"[{s.location}]\n{s.text}" for s in sections)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    return FileText(record.path, names, text, truncated)


# ---- similar documents -------------------------------------------------------
#
# Two layers, like note-similarity tools in note apps:
#   1. Document level: each file has one vector (the mean of its chunk vectors).
#      Files whose vector is nearest the source file's are candidates.
#   2. Passage level: some of the source's passages are also searched on their
#      own, so a file that shares one strong section (but is otherwise about
#      something else) is still found. Every candidate is then scored on its
#      best passage matches, which are returned as evidence.

DOC_CANDIDATES = 40  # files taken from the document-level ranking
PROBE_PASSAGES = 16  # source passages searched one by one for passage-level candidates
PROBE_HITS = 10  # passages kept per probe
TOP_BLOCKS = 3  # a candidate's passage score is the mean of its best few passage matches
DOC_WEIGHT = 0.5  # final score = DOC_WEIGHT * document similarity + rest * passage similarity
PASSAGE_CHARS = 500  # evidence passages are cut to this length


class NothingToCompare(Exception):
    def __init__(self, path: str):
        super().__init__(f"{path} has no indexed text to compare with other documents.")


@dataclass(frozen=True)
class Evidence:
    text: str  # passage in the similar file
    location: str  # where it is in the similar file
    source_location: str  # the passage of the source file it matches best
    similarity: float


@dataclass(frozen=True)
class SimilarFile:
    path: str
    collections: tuple[str, ...]
    score: float
    document_similarity: float
    passage_similarity: float
    passages: tuple[Evidence, ...]


@dataclass(frozen=True)
class SimilarResult:
    source: str
    source_collections: tuple[str, ...]
    results: tuple[SimilarFile, ...]


def find_similar(
    store: Store, path: str, active: Iterable[Collection], limit: int = 8, passages: int = 2
) -> SimilarResult:
    """Files in the active collections most like the file at `path`, which must be in one too."""
    import numpy as np

    active = [c for c in active if c.active]
    record, names = _allowed_record(store, path, active)
    _, src_locs, _, src = store.chunk_vectors(record.id)
    doc = store.doc_vector(record.id)
    if doc is None or len(src) == 0:
        raise NothingToCompare(record.path)
    allowed = allowed_files(store, active)
    allowed.pop(record.id, None)
    if not allowed or limit <= 0:
        return SimilarResult(record.path, names, ())
    ids = list(allowed)

    # Layer 1 and 2: gather candidate files.
    doc_sim = dict(store.similar_files(doc, ids, max(DOC_CANDIDATES, limit * 3)))
    candidates = set(doc_sim)
    for row in _spread(len(src), PROBE_PASSAGES):
        hits = store.vector_search(src[row].tolist(), ids, PROBE_HITS)
        candidates.update(_file_ids(store, [h.chunk_id for h in hits]).values())

    src = _unit(src)
    scored = []
    for file_id in candidates:
        _, locs, texts, mat = store.chunk_vectors(file_id)
        if len(mat) == 0:
            continue
        sims = src @ _unit(mat).T  # (source passages, candidate passages)
        best = sims.max(axis=0)  # each candidate passage's best match in the source
        order = np.argsort(-best)
        passage_sim = float(best[order[:TOP_BLOCKS]].mean())
        if file_id not in doc_sim:
            other = store.doc_vector(file_id)
            doc_sim[file_id] = float(np.dot(doc, other)) if other is not None else 0.0
        score = DOC_WEIGHT * doc_sim[file_id] + (1 - DOC_WEIGHT) * passage_sim
        evidence = tuple(
            Evidence(_clip(texts[i]), locs[i], src_locs[int(sims[:, i].argmax())], round(float(best[i]), 4))
            for i in order[:max(0, passages)]
        )
        scored.append((score, file_id, passage_sim, evidence))

    scored.sort(key=lambda s: s[0], reverse=True)
    paths = {rec.id: rec.path for rec in store.files().values()}
    results = tuple(
        SimilarFile(paths[fid], allowed[fid], round(score, 4), round(doc_sim[fid], 4), round(psim, 4), ev)
        for score, fid, psim, ev in scored[:limit]
    )
    return SimilarResult(record.path, names, results)


def _spread(n: int, k: int) -> list[int]:
    """Up to k row numbers spread evenly over 0..n-1."""
    if n <= k:
        return list(range(n))
    return sorted({round(i * (n - 1) / (k - 1)) for i in range(k)})


def _unit(m):
    import numpy as np

    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= PASSAGE_CHARS else text[:PASSAGE_CHARS].rsplit(" ", 1)[0] + "…"
