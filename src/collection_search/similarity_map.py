"""A 2D map of a collection where documents about the same thing sit together.

Built from the document vectors the index already has (one per file), so drawing
a map embeds nothing and reads no files. It works in three steps:

1. Group the documents into themes with k-means on their vectors (cosine).
2. Lay the themes out so similar themes are near each other (classical MDS on
   the distances between theme centres), each as a disc sized by its document
   count, pushed apart until no two discs overlap.
3. Place each theme's documents inside its disc by the theme's own two main
   axes of variation, so sub-topics show up as clumps within the disc.

Themes are named from words that are common in their most typical documents
but rare in the other themes. 35,000 documents take a few seconds, all numpy.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable

import numpy as np

from .collections import Collection
from .search import allowed_files
from .store import Store

MAX_THEMES = 16
NAME_SAMPLE = 25  # typical documents per theme read for naming it
NAME_WORDS = 3
COLORS = 8  # palette slots; neighbouring themes never share one
SEED = 7  # fixed, so the same index draws the same map
PASSAGE_LIMIT = 20_000  # passages drawn in passage mode; more are sampled evenly down to this
NEIGHBOURS = 5  # closest passages in other files kept per passage
STRONGEST = 400  # links drawn across the whole passage map when nothing is selected
SAME_TEXT = 0.97  # passages at least this similar are near-copies (quotes, boilerplate)

STOPWORDS = set("""
about above after again against all also although among and another any are aren't around as at
be because been before being below between both but by can cannot could did does doing done down
during each either else even ever every few for from further had has have having he her here hers
herself him himself his how however i if in into is isn't it its itself just least less let like
made make many may me might more most much must my myself neither no nor not now of off often on
once one only or other others otherwise our ours ourselves out over own per perhaps rather same
shall she should since so some such than that the their theirs them themselves then there these
they this those though through thus to too toward under until up upon us use used using very was
we were what when where whether which while who whom whose why will with within without would yet
you your yours yourself yourselves also page pages section chapter part see note notes www http https
com org html pdf docx doc txt md file files new one two three first second third get got
""".split())


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Theme:
    index: int
    label: str  # a few distinctive words, e.g. "covenant, sinai, law"
    size: int
    x: float  # disc centre on the map
    y: float
    radius: float
    color: int  # palette slot, 0..COLORS-1
    folder: str = ""  # the folder most of its documents are in, when there is one


@dataclass
class SimilarityMap:
    """One dot per document, or (in passage mode) one per passage."""

    paths: list[str]
    collections: list[tuple[str, ...]]
    xy: np.ndarray  # (n, 2) positions
    theme: np.ndarray  # (n,) theme index of each dot
    typicality: np.ndarray  # (n,) cosine similarity to its theme's centre
    themes: list[Theme]
    key: tuple = field(default=(), compare=False)
    # Passage mode only:
    passages: bool = False
    locations: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    total: int = 0  # passages in scope; more than len(self) when only a sample is drawn
    neighbours: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.int32))  # (n, k), -1 = none
    neighbour_sim: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))
    strongest: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), np.float32))  # rows (i, j, similarity)

    def __len__(self) -> int:
        return len(self.paths)

    def members(self, theme: int) -> np.ndarray:
        """Documents of one theme, most typical first."""
        idx = np.flatnonzero(self.theme == theme)
        return idx[np.argsort(-self.typicality[idx], kind="stable")]

    def near(self, i: int, count: int = 50) -> np.ndarray:
        """The documents of i's theme closest to it on the map, i first."""
        idx = np.flatnonzero(self.theme == self.theme[i])
        d = np.linalg.norm(self.xy[idx] - self.xy[i], axis=1)
        return idx[np.argsort(d, kind="stable")[:count]]


def empty_map(key: tuple = ()) -> SimilarityMap:
    return SimilarityMap([], [], np.zeros((0, 2), np.float32), np.zeros(0, np.int32), np.zeros(0, np.float32), [],
                         key)


def theme_count(n: int) -> int:
    if n < 6:
        return 1
    return int(min(MAX_THEMES, max(2, round(math.sqrt(n / 4)))))


def build_map(store: Store, active: Iterable[Collection], key: tuple = (),
              cancelled: Callable[[], bool] = lambda: False) -> SimilarityMap:
    allowed = allowed_files(store, active)
    ids, paths, vectors = store.doc_vectors(list(allowed))
    if not ids:
        return empty_map(key)

    def texts(indices: list[int]) -> list[str]:
        opening = store.opening_text([ids[i] for i in indices])
        return [_stem(paths[i]) + " " + opening.get(ids[i], "") for i in indices]

    return _build(vectors, paths, [allowed[i] for i in ids], texts, key, cancelled)


def build_passage_map(store: Store, active: Iterable[Collection], key: tuple = (),
                      cancelled: Callable[[], bool] = lambda: False, limit: int = PASSAGE_LIMIT) -> SimilarityMap:
    """One dot per passage, with each passage's closest passages in other files.

    Past `limit` passages an even sample is drawn, so the work stays bounded (comparing every
    passage with every other grows with the square of the count).
    """
    allowed = allowed_files(store, active)
    chunk_ids, file_ids = store.passage_ids(list(allowed))
    total = len(chunk_ids)
    if not total:
        return replace(empty_map(key), passages=True)
    if total > limit:
        keep = np.unique(np.linspace(0, total - 1, limit).round().astype(np.int64))
        chunk_ids, file_ids = chunk_ids[keep], file_ids[keep]
    locations, headings, texts, vectors = store.passages(chunk_ids.tolist())
    paths_by_id = {rec.id: rec.path for rec in store.files().values()}
    paths = [paths_by_id[int(f)] for f in file_ids]
    if cancelled():
        raise Cancelled

    def name_texts(indices: list[int]) -> list[str]:
        return [f"{headings[i]} {texts[i]}" for i in indices]

    m = _build(vectors, paths, [allowed[int(f)] for f in file_ids], name_texts, key, cancelled)
    x = _unit(vectors.astype(np.float32))
    neighbours, neighbour_sim = _neighbours(x, file_ids, NEIGHBOURS, cancelled)
    return replace(m, passages=True, locations=locations,
                   texts=[(h + "\n" if h else "") + t for h, t in zip(headings, texts)], total=total,
                   neighbours=neighbours, neighbour_sim=neighbour_sim, strongest=_strongest(neighbours, neighbour_sim))


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _build(vectors: np.ndarray, paths: list[str], collections: list[tuple[str, ...]],
           texts: Callable[[list[int]], list[str]], key: tuple, cancelled: Callable[[], bool]) -> SimilarityMap:
    n = len(paths)
    x = _unit(vectors.astype(np.float32))
    rng = np.random.default_rng(SEED)
    labels, centres = _kmeans(x, theme_count(n), rng, cancelled)

    # Biggest theme first; drop any that ended up empty.
    sizes = np.bincount(labels, minlength=len(centres))
    order = [t for t in np.argsort(-sizes, kind="stable") if sizes[t] > 0]
    remap = np.full(len(centres), -1)
    remap[order] = np.arange(len(order))
    labels, centres, sizes = remap[labels], centres[order], sizes[order]
    typicality = np.einsum("ij,ij->i", x, centres[labels]).astype(np.float32)
    if cancelled():
        raise Cancelled

    centre_xy, radii = _layout_themes(centres, sizes)
    xy = np.zeros((n, 2), np.float32)
    for t in range(len(centres)):
        idx = np.flatnonzero(labels == t)
        xy[idx] = centre_xy[t] + _place_in_disc(x[idx] - centres[t], radii[t])
    if cancelled():
        raise Cancelled

    names = _name_themes(texts, paths, labels, typicality, len(centres))
    colors = _colors(centre_xy, radii)
    themes = [
        Theme(t, names[t][0], int(sizes[t]), float(centre_xy[t, 0]), float(centre_xy[t, 1]), float(radii[t]),
              colors[t], names[t][1])
        for t in range(len(centres))
    ]
    return SimilarityMap(paths, collections, xy, labels.astype(np.int32), typicality, themes, key)


# ---- passage neighbours ----------------------------------------------------------
def _neighbours(x: np.ndarray, file_ids: np.ndarray, k: int,
                cancelled: Callable[[], bool]) -> tuple[np.ndarray, np.ndarray]:
    """Each passage's k most similar passages in other files (exact, in blocks)."""
    n = len(x)
    k = min(k, max(n - 1, 0))
    nbr = np.full((n, k), -1, np.int32)
    sim = np.zeros((n, k), np.float32)
    if k == 0:
        return nbr, sim
    for start in range(0, n, 1024):
        if cancelled():
            raise Cancelled
        block = x[start:start + 1024] @ x.T
        block[file_ids[start:start + 1024, None] == file_ids[None, :]] = -2.0  # same file never counts
        top = np.argpartition(-block, k - 1, axis=1)[:, :k] if k < n else np.tile(np.arange(n), (len(block), 1))
        vals = np.take_along_axis(block, top, axis=1)
        order = np.argsort(-vals, axis=1, kind="stable")
        top, vals = np.take_along_axis(top, order, axis=1), np.take_along_axis(vals, order, axis=1)
        top[vals <= -1.5] = -1
        nbr[start:start + len(block)] = top
        sim[start:start + len(block)] = np.where(vals <= -1.5, 0, vals)
    return nbr, sim


def _strongest(nbr: np.ndarray, sim: np.ndarray, count: int = STRONGEST) -> np.ndarray:
    """The strongest links between passages of different files, each pair once, near-copies left out."""
    if nbr.size == 0:
        return np.zeros((0, 3), np.float32)
    rows = np.repeat(np.arange(len(nbr)), nbr.shape[1])
    cols, vals = nbr.ravel(), sim.ravel()
    ok = (cols >= 0) & (vals < SAME_TEXT)
    a, b = np.minimum(rows[ok], cols[ok]), np.maximum(rows[ok], cols[ok])
    pairs, first = np.unique(np.stack([a, b], axis=1), axis=0, return_index=True)
    vals = vals[ok][first]
    top = np.argsort(-vals, kind="stable")[:count]
    return np.column_stack([pairs[top], vals[top]]).astype(np.float32)


# ---- grouping -----------------------------------------------------------------
def _unit(m: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


def _assign(x: np.ndarray, centres: np.ndarray) -> np.ndarray:
    out = np.empty(len(x), np.int64)
    for s in range(0, len(x), 8192):
        out[s:s + 8192] = (x[s:s + 8192] @ centres.T).argmax(axis=1)
    return out


def _kmeans(x: np.ndarray, k: int, rng: np.random.Generator, cancelled: Callable[[], bool],
            iterations: int = 30, tries: int = 4) -> tuple[np.ndarray, np.ndarray]:
    """Spherical k-means (cosine). The best of a few k-means++ starts on a sample, then refined on all."""
    n = len(x)
    k = min(k, n)
    if k <= 1:
        return np.zeros(n, np.int64), _unit(x.mean(axis=0, keepdims=True))
    sample = x if n <= 5000 else x[rng.choice(n, 5000, replace=False)]
    best, best_fit = None, -np.inf
    for _ in range(tries):
        centres = _refine(sample, _seed(sample, k, rng), 10, cancelled)[1]
        fit = float((sample @ centres.T).max(axis=1).sum())
        if fit > best_fit:
            best, best_fit = centres, fit
    return _refine(x, best, iterations, cancelled)


def _seed(x: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """k-means++ starting centres."""
    centres = [x[rng.integers(len(x))]]
    dist = np.clip(1 - x @ centres[0], 0, None)
    for _ in range(1, k):
        total = dist.sum()
        pick = rng.choice(len(x), p=dist / total) if total > 0 else rng.integers(len(x))
        centres.append(x[pick])
        dist = np.minimum(dist, np.clip(1 - x @ x[pick], 0, None))
    return np.stack(centres)


def _refine(x: np.ndarray, centres: np.ndarray, iterations: int,
            cancelled: Callable[[], bool]) -> tuple[np.ndarray, np.ndarray]:
    c = centres.copy()
    labels = None
    for _ in range(iterations):
        if cancelled():
            raise Cancelled
        new = _assign(x, c)
        if labels is not None and np.array_equal(new, labels):
            break
        labels = new
        for t in range(len(c)):
            members = x[labels == t]
            if len(members):
                c[t] = members.sum(axis=0)
            else:  # restart an empty group at the document worst served by its group
                c[t] = x[int(np.argmin(np.einsum("ij,ij->i", x, c[labels])))]
        c = _unit(c)
    return _assign(x, c), c


# ---- layout -------------------------------------------------------------------
def _layout_themes(centres: np.ndarray, sizes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Disc centre and radius of each theme: similar themes close, no overlaps, disc area ~ size."""
    k = len(centres)
    radii = np.sqrt(sizes / sizes.sum()).astype(np.float64)
    if k == 1:
        return np.zeros((1, 2)), radii
    d = np.clip(1 - centres @ centres.T, 0, 2).astype(np.float64)
    pos = _mds(d)
    span = np.abs(pos).max()
    pos = pos / span * 1.5 if span > 0 else pos
    gap = 0.04
    for step in range(400):
        pos *= 0.995  # gravity keeps the map compact
        for _ in range(3):
            moved = False
            for i in range(k):
                for j in range(i + 1, k):
                    delta = pos[j] - pos[i]
                    dist = float(np.hypot(*delta))
                    need = radii[i] + radii[j] + gap
                    if dist >= need:
                        continue
                    if dist < 1e-9:
                        angle = (i * 2.399 + j) % (2 * math.pi)  # deterministic nudge
                        delta, dist = np.array([math.cos(angle), math.sin(angle)]), 1.0
                    push = (need - dist) * delta / dist
                    wi = radii[j] ** 2 / (radii[i] ** 2 + radii[j] ** 2)  # small discs move more
                    pos[i] -= push * wi
                    pos[j] += push * (1 - wi)
                    moved = True
            if not moved:
                break
    pos -= (pos * (radii ** 2)[:, None]).sum(axis=0) / (radii ** 2).sum()
    return pos, radii


def _mds(d: np.ndarray) -> np.ndarray:
    """Classical multidimensional scaling to 2D."""
    k = len(d)
    j = np.eye(k) - 1.0 / k
    b = -0.5 * j @ (d ** 2) @ j
    values, vectors = np.linalg.eigh(b)
    top = np.argsort(values)[::-1][:2]
    pos = vectors[:, top] * np.sqrt(np.clip(values[top], 1e-12, None))
    for axis in range(pos.shape[1]):  # fix the sign so the map doesn't flip between redraws
        if pos[np.argmax(np.abs(pos[:, axis])), axis] < 0:
            pos[:, axis] *= -1
    if pos.shape[1] < 2:
        pos = np.hstack([pos, np.zeros((k, 2 - pos.shape[1]))])
    return pos


def _place_in_disc(offsets: np.ndarray, radius: float) -> np.ndarray:
    """Positions inside a disc of `radius`, from the group's own two main axes of variation.

    Each axis is spread evenly by rank, so the disc fills whatever the theme's shape, and
    documents keep their order along both axes: a theme's sub-topics land in different parts of it.
    """
    m = len(offsets)
    if m == 1:
        return np.zeros((1, 2), np.float32)
    centred = offsets - offsets.mean(axis=0)
    if m <= 2000:
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        axes = vt[:2].T
    else:
        values, vectors = np.linalg.eigh(centred.T @ centred)
        axes = vectors[:, np.argsort(values)[::-1][:2]]
    if axes.shape[1] < 2:
        axes = np.hstack([axes, np.zeros((axes.shape[0], 1))])
    for a in range(2):  # stable orientation between redraws
        if axes[np.argmax(np.abs(axes[:, a])), a] < 0:
            axes[:, a] *= -1
    p = centred @ axes
    # Spread each axis evenly by rank (keeps the order along it), then map the square onto the disc.
    u = np.empty((m, 2))
    for a in range(2):
        u[np.argsort(p[:, a], kind="stable"), a] = (np.arange(m) + 0.5) / m * 2 - 1
    x = u[:, 0] * np.sqrt(1 - u[:, 1] ** 2 / 2)
    y = u[:, 1] * np.sqrt(1 - u[:, 0] ** 2 / 2)
    return (np.stack([x, y], axis=1) * radius * 0.95).astype(np.float32)


def _colors(pos: np.ndarray, radii: np.ndarray) -> list[int]:
    """Palette slot per theme, biggest first, so themes that touch never share a colour."""
    k = len(pos)
    colors: list[int] = []
    for i in range(k):
        near = {colors[j] for j in range(i)
                if np.hypot(*(pos[i] - pos[j])) < (radii[i] + radii[j]) * 1.6}
        colors.append(next((c for c in range(COLORS) if c not in near), i % COLORS))
    return colors


# ---- names --------------------------------------------------------------------
_WORD = re.compile(r"[^\W\d_]{3,}")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in STOPWORDS and len(w) <= 24}


def _name_themes(texts: Callable[[list[int]], list[str]], paths: list[str], labels: np.ndarray,
                 typicality: np.ndarray, k: int) -> list[tuple[str, str]]:
    """(label, main folder) per theme."""
    samples: list[np.ndarray] = []
    for t in range(k):
        idx = np.flatnonzero(labels == t)
        samples.append(idx[np.argsort(-typicality[idx], kind="stable")[:NAME_SAMPLE]])
    sample_texts = texts([int(i) for s in samples for i in s])
    doc_words: list[list[set[str]]] = []
    overall: Counter = Counter()
    pos = 0
    for sample in samples:
        sets = []
        for _ in sample:
            words = _words(sample_texts[pos])
            pos += 1
            sets.append(words)
            overall.update(words)
        doc_words.append(sets)
    total = sum(len(s) for s in doc_words)

    names = []
    for t in range(k):
        sets = doc_words[t]
        here = Counter(w for s in sets for w in s)
        least = 2 if len(sets) >= 4 else 1
        scored = sorted(
            ((count / len(sets)) * math.log((total + 1) / (overall[w] + 1)), w)
            for w, count in here.items()
            if count >= least and (k == 1 or overall[w] <= 0.5 * total)  # words everywhere name nothing
        )
        words = [w for _, w in reversed(scored)][:NAME_WORDS]
        label = ", ".join(words) if words else f"Group {t + 1}"
        idx = np.flatnonzero(labels == t)
        folders = Counter(os.path.basename(os.path.dirname(paths[i])) for i in idx)
        folder, count = folders.most_common(1)[0]
        names.append((label, folder if count >= 0.5 * len(idx) else ""))
    return names
