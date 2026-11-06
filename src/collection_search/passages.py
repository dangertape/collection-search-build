"""Which passages of one document are about the same thing as passages in other documents.

The candidates are found the same way as for find_similar (nearest documents by
their document vector, plus a nearest-passage search for a spread of the
document's own passages), then every passage of the document is compared with
every passage of the candidates. Only files in the given collections are used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .collections import Collection
from .search import (DOC_CANDIDATES, PROBE_HITS, PROBE_PASSAGES, NothingToCompare, _allowed_record, _clip,
                     _file_ids, _spread, _unit, allowed_files)
from .store import Store

PER_PASSAGE = 3  # links kept for each passage of the document
MIN_SIMILARITY = 0.6  # weaker matches are left out (cosine; the default model scores related text ~0.6-0.9)
SAME_TEXT = 0.97  # at or above this, the two passages are (nearly) the same words


@dataclass(frozen=True)
class PassageLink:
    path: str
    location: str
    heading: str
    text: str
    similarity: float
    same_text: bool  # nearly identical wording, e.g. a quote or shared boilerplate


@dataclass(frozen=True)
class SourcePassage:
    location: str
    heading: str
    text: str
    links: tuple[PassageLink, ...]


@dataclass(frozen=True)
class PassageLinks:
    source: str
    passages: tuple[SourcePassage, ...]

    def linked_paths(self) -> dict[str, float]:
        """Each linked document and its strongest link, strongest first."""
        best: dict[str, float] = {}
        for p in self.passages:
            for link in p.links:
                best[link.path] = max(best.get(link.path, 0.0), link.similarity)
        return dict(sorted(best.items(), key=lambda kv: -kv[1]))


def passage_links(store: Store, path: str, active: Iterable[Collection], per_passage: int = PER_PASSAGE,
                  min_similarity: float = MIN_SIMILARITY) -> PassageLinks:
    """For each passage of the file at `path`, the closest passages in other files of the collections."""
    active = [c for c in active if c.active]
    record, _ = _allowed_record(store, path, active)
    _, src_locs, src_heads, src_texts, src = store.chunks_of([record.id])
    doc = store.doc_vector(record.id)
    if doc is None or len(src) == 0:
        raise NothingToCompare(record.path)
    allowed = allowed_files(store, active)
    allowed.pop(record.id, None)
    if not allowed:
        return PassageLinks(record.path, tuple(SourcePassage(loc, head, _clip(text), ())
                                               for loc, head, text in zip(src_locs, src_heads, src_texts)))
    ids = list(allowed)
    candidates = {fid for fid, _ in store.similar_files(doc, ids, DOC_CANDIDATES)}
    for row in _spread(len(src), PROBE_PASSAGES):
        hits = store.vector_search(src[row].tolist(), ids, PROBE_HITS)
        candidates.update(_file_ids(store, [h.chunk_id for h in hits]).values())
    files, locs, heads, texts, mat = store.chunks_of(sorted(candidates))
    paths = {rec.id: rec.path for rec in store.files().values()}

    sims = _unit(src) @ _unit(mat).T if len(mat) else np.zeros((len(src), 0))
    out = []
    for i in range(len(src)):
        row = sims[i]
        order = np.argsort(-row, kind="stable")
        links, seen = [], set()
        for j in order:
            if row[j] < min_similarity or len(links) >= per_passage:
                break
            if files[j] in seen:  # one link per other file, so one long file can't take every slot
                continue
            seen.add(files[j])
            links.append(PassageLink(paths[int(files[j])], locs[j], heads[j], _clip(texts[j]),
                                     round(float(row[j]), 4), bool(row[j] >= SAME_TEXT)))
        out.append(SourcePassage(src_locs[i], src_heads[i], _clip(src_texts[i]), tuple(links)))
    return PassageLinks(record.path, tuple(out))
