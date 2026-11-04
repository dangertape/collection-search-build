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


class LocationNotFound(Exception):
    def __init__(self, path: str, around: str):
        super().__init__(f"Could not find {around!r} in {path}. Use a location or heading from a search result, "
                         "or leave 'around' out to read the whole file.")


@dataclass(frozen=True)
class FileText:
    path: str
    collections: tuple[str, ...]
    text: str
    truncated: bool
    shown: str = ""  # which part was returned, when only part was asked for

RRF_K = 60  # standard reciprocal-rank-fusion constant
CANDIDATES = 60  # per method, before fusion (more when a later page is asked for)
PER_FILE = 3  # at most this many passages from one file per page, so one file can't fill the list


@dataclass(frozen=True)
class Result:
    text: str
    path: str
    collections: tuple[str, ...]
    location: str
    score: float
    heading: str = ""  # headings above the passage, e.g. "Exodus 20 > 4-6"


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
    offset: int = 0,
    per_file: int = PER_FILE,
) -> list[Result]:
    return search_page(store, embedder, query, active, limit, use_keywords, offset, per_file)[0]


def search_page(
    store: Store,
    embedder: Embedder,
    query: str,
    active: Iterable[Collection],
    limit: int = 8,
    use_keywords: bool = True,
    offset: int = 0,
    per_file: int = PER_FILE,
) -> tuple[list[Result], bool]:
    """One page of results, and whether there are more after it."""
    query = query.strip()
    offset = max(0, offset)
    if not query or limit <= 0:
        return [], False
    allowed = allowed_files(store, active)
    if not allowed:
        return [], False
    ids = list(allowed)
    wanted = offset + limit + 1
    candidates = max(CANDIDATES, wanted * 3)
    semantic = store.vector_search(embedder.embed_query(query), ids, candidates)
    keyword = store.keyword_search(query, ids, candidates) if use_keywords else []
    fused = _spread_files(_fuse([semantic, keyword]), per_file)[offset:offset + limit + 1]
    more = len(fused) > limit
    results = [
        Result(hit.text, hit.path, allowed[hit.file_id], hit.location, round(score, 5), hit.heading)
        for hit, score in fused[:limit]
    ]
    return results, more


def _spread_files(ranked: list[tuple[ChunkHit, float]], per_file: int) -> list[tuple[ChunkHit, float]]:
    """Keep the ranking, but move a file's passages beyond the first `per_file` to the end.

    Nothing is dropped, so paging further still reaches them.
    """
    if per_file <= 0:
        return ranked
    seen: dict[int, int] = {}
    first, later = [], []
    for item in ranked:
        n = seen.get(item[0].file_id, 0)
        seen[item[0].file_id] = n + 1
        (first if n < per_file else later).append(item)
    return first + later


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


def read_file(
    store: Store,
    path: str,
    active: Iterable[Collection],
    limit: int = READ_LIMIT,
    around: str | None = None,
    context: int = 4,
) -> FileText:
    """Text of one indexed file, only if an active collection allows it.

    With `around` (a location like "paragraphs 12-15" or a heading like
    "Exodus 20 > 4"), only that part and `context` sections either side are returned.
    """
    record, names = _allowed_record(store, path, active)
    try:
        sections = parse(record.path)
    except ParseError as exc:
        raise ParseError(f"Could not read {record.path}: {exc}") from exc
    shown = ""
    if around and around.strip():
        span = find_span(sections, around.strip())
        if span is None:
            raise LocationNotFound(record.path, around)
        first = max(0, span[0] - max(0, context))
        last = min(len(sections) - 1, span[1] + max(0, context))
        shown = f"sections {first + 1}-{last + 1} of {len(sections)}"
        sections = sections[first:last + 1]
    text = "\n\n".join(_marker(s) + "\n" + s.text for s in sections)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    return FileText(record.path, names, text, truncated, shown)


def _marker(section) -> str:
    return f"[{section.location} | {section.heading}]" if section.heading else f"[{section.location}]"


def find_span(sections, around: str) -> tuple[int, int] | None:
    """First and last section index that a location or heading from a result refers to."""
    import re

    numbered: dict[tuple[str, int], int] = {}
    for i, s in enumerate(sections):
        m = re.match(r"(page|paragraph) (\d+)", s.location)
        if m:
            numbered.setdefault((m.group(1), int(m.group(2))), i)

    def one(loc: str) -> int | None:
        loc = loc.strip()
        m = re.match(r"(page|paragraph)s? (\d+)", loc)
        if m and (m.group(1), int(m.group(2))) in numbered:
            return numbered[(m.group(1), int(m.group(2)))]
        for i, s in enumerate(sections):
            if s.location == loc:
                return i
        return None

    m = re.fullmatch(r"(page|paragraph)s (\d+)-(\d+)", around)
    if m:
        a, b = one(f"{m.group(1)} {m.group(2)}"), one(f"{m.group(1)} {m.group(3)}")
        if a is not None and b is not None:
            return a, b
    if " to " in around:
        a_text, b_text = around.split(" to ", 1)
        a, b = one(a_text), one(b_text)
        if a is not None and b is not None:
            return a, b
    single = one(around)
    if single is not None:
        return single, single
    # A heading range from a result, "Exodus 20 > 4-6": from the first heading to the last.
    head, _, last_part = around.rpartition(" > ")
    if "-" in last_part:
        a_text, b_text = last_part.split("-", 1)
        prefix = head + " > " if head else ""
        a, b = _heading_span(sections, prefix + a_text), _heading_span(sections, prefix + b_text)
        if a and b and a[0] <= b[1]:
            return a[0], b[1]
    return _heading_span(sections, around)


def _heading_span(sections, around: str) -> tuple[int, int] | None:
    """The run of sections under a heading, e.g. "Exodus 20 > 4", or just its last part, "4"."""
    wanted = around.lower().strip()
    for test in (lambda h: h == wanted, lambda h: h.endswith(" > " + wanted), lambda h: wanted in h):
        hits = [i for i, s in enumerate(sections) if s.heading and test(s.heading.lower())]
        if hits:
            run = [hits[0]]
            for i in hits[1:]:
                if i != run[-1] + 1:
                    break
                run.append(i)
            return run[0], run[-1]
    return None


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


# ---- links between documents ---------------------------------------------------
#
# Links and verse references are stored per file while indexing (links.py).
# Here they are matched up with the files in the active collections, so a
# link never leads outside the user's scope.

LINK_MENTIONS = 5  # mentions listed per linked file


def _target_index(store: Store, allowed: dict[int, tuple[str, ...]]) -> tuple[dict[str, list[int]], dict[int, str]]:
    """link key -> allowed file ids it reaches, and file id -> path."""
    from .links import file_keys

    index: dict[str, list[int]] = {}
    paths: dict[int, str] = {}
    for rec in store.files().values():
        if rec.id not in allowed:
            continue
        paths[rec.id] = rec.path
        for key in file_keys(rec.path):
            index.setdefault(key, []).append(rec.id)
    return index, paths


def _mention(row) -> dict:
    out = {"kind": "verse reference" if row.kind == "verse" else "link", "text": row.label, "location": row.location}
    if row.heading:
        out["heading"] = row.heading
    return out


def _outgoing(store, file_id, index) -> tuple[dict[int, list], list]:
    found: dict[int, list] = {}
    unresolved = []
    for row in store.links_from(file_id):
        targets = [t for t in index.get(row.target, []) if t != file_id]
        if not targets:
            unresolved.append(row)
        for t in targets:
            found.setdefault(t, []).append(row)
    return found, unresolved


def _incoming(store, file_id, path, allowed) -> dict[int, list]:
    from .links import file_keys

    found: dict[int, list] = {}
    for row in store.links_to(file_keys(path)):
        if row.file_id != file_id and row.file_id in allowed:
            found.setdefault(row.file_id, []).append(row)
    return found


def document_links(store: Store, path: str, active: Iterable[Collection], limit: int = 30) -> dict:
    """What a file links to, what links back to it, and the verses it mentions."""
    active = [c for c in active if c.active]
    record, names = _allowed_record(store, path, active)
    allowed = allowed_files(store, active)
    index, paths = _target_index(store, allowed)
    outgoing, unresolved = _outgoing(store, record.id, index)
    incoming = _incoming(store, record.id, record.path, allowed)

    def listing(found: dict[int, list]) -> list[dict]:
        ordered = sorted(found.items(), key=lambda kv: (-len(kv[1]), paths[kv[0]].lower()))
        return [
            {"path": paths[fid], "collections": list(allowed[fid]), "mentions": len(rows),
             "examples": [_mention(r) for r in rows[:LINK_MENTIONS]]}
            for fid, rows in ordered[:limit]
        ]

    verses = [r.label for r in store.links_from(record.id) if r.kind == "verse"]
    return {
        "path": record.path,
        "collections": list(names),
        "links_to": listing(outgoing),
        "linked_from": listing(incoming),
        "links_to_total": len(outgoing),
        "linked_from_total": len(incoming),
        "verses_mentioned": list(dict.fromkeys(verses))[:100],
        "links_not_found": list(dict.fromkeys(r.label for r in unresolved))[:30],
    }


# ---- explore: walk outward from a starting point ---------------------------------

EXPLORE_SEEDS = 5  # files a query starts from
EXPLORE_SIMILAR = 3  # similar documents followed from each file
EXPLORE_EDGES = 80


def explore(
    store: Store,
    embedder: Embedder | None,
    active: Iterable[Collection],
    query: str | None = None,
    path: str | None = None,
    steps: int = 2,
    max_documents: int = 15,
) -> dict:
    """Start from a search or a file, then follow links, backlinks, verse references
    and similar documents outward, up to `steps` hops and `max_documents` files."""
    import numpy as np

    active = [c for c in active if c.active]
    allowed = allowed_files(store, active)
    query = (query or "").strip()
    qvec = np.asarray(embedder.embed_query(query), dtype=np.float32) if query and embedder else None
    if path:
        record, _ = _allowed_record(store, path, active)
        seeds = [record.id]
    elif query:
        hits, _ = search_page(store, embedder, query, active, EXPLORE_SEEDS, per_file=1)
        keys = {rec.path: rec.id for rec in store.files().values()}
        seeds = list(dict.fromkeys(keys[h.path] for h in hits if h.path in keys))
    else:
        raise ValueError("Give a query or a path to start from.")
    if not allowed:
        return {"start": path or query, "documents": [], "connections": []}
    index, paths = _target_index(store, allowed)
    ids = list(allowed)

    step_of: dict[int, int] = {fid: 0 for fid in seeds[:max(1, max_documents)] if fid in allowed}
    reached: dict[int, list[str]] = {fid: ["starting point"] for fid in step_of}
    edges: list[dict] = []
    seen_edges: set[tuple[int, int, str]] = set()

    def connect(a: int, b: int, src: int, dst: int, how: str, step: int) -> None:
        """Reach file b from file a; the connection itself reads "src <how> dst"."""
        if b not in step_of:
            if len(step_of) >= max_documents:
                return
            step_of[b] = step
        key = (min(src, dst), max(src, dst), "similar") if how.startswith("is similar") else (src, dst, how)
        if key in seen_edges:
            return
        seen_edges.add(key)
        reached.setdefault(b, []).append(f"{os.path.basename(paths[src])} {how} {os.path.basename(paths[dst])}")
        if len(edges) < EXPLORE_EDGES:
            edges.append({"from": paths[src], "how": how, "to": paths[dst]})

    frontier = list(step_of)
    for step in range(1, max(1, steps) + 1):
        nxt = []
        for fid in frontier:
            before = set(step_of)
            outgoing, _ = _outgoing(store, fid, index)
            for target, rows in outgoing.items():
                how = "mentions verses in" if all(r.kind == "verse" for r in rows) else "links to"
                connect(fid, target, fid, target, how, step)
            for source, rows in _incoming(store, fid, paths[fid], allowed).items():
                how = "mentions verses in" if all(r.kind == "verse" for r in rows) else "links to"
                connect(fid, source, source, fid, how, step)
            doc = store.doc_vector(fid)
            if doc is not None:
                for other, sim in store.similar_files(doc, ids, EXPLORE_SIMILAR + 1):
                    if other != fid:
                        connect(fid, other, fid, other, f"is similar ({sim:.2f}) to", step)
            nxt.extend(f for f in step_of if f not in before)
        frontier = nxt
        if not frontier or len(step_of) >= max_documents:
            break

    documents = []
    for fid, step in step_of.items():
        _, locs, texts, mat = store.chunk_vectors(fid)
        headings = {loc: head for loc, head, _ in store.chunk_headings(fid)}
        best, relevance = 0, None
        if len(mat) and qvec is not None:
            sims = _unit(mat) @ (qvec / (np.linalg.norm(qvec) or 1.0))
            best = int(sims.argmax())
            relevance = round(float(sims[best]), 4)
        entry = {"path": paths[fid], "collections": list(allowed[fid]), "step": step,
                 "reached_by": reached.get(fid, [])[:4]}
        if relevance is not None:
            entry["relevance"] = relevance
        if texts:
            entry["passage"] = {"text": _clip(texts[best]), "location": locs[best]}
            if headings.get(locs[best]):
                entry["passage"]["heading"] = headings[locs[best]]
        documents.append(entry)
    documents.sort(key=lambda d: (d["step"] > 0, -(d.get("relevance") or 0), d["step"]))
    return {"start": path or query, "documents": documents, "connections": edges}
