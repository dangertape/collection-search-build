"""Passage-level connections: one document's links, and the passage view of the map."""

import os
import time

import numpy as np
import pytest

from collection_search.collections import Collection
from collection_search.indexer import sync
from collection_search.passages import passage_links
from collection_search.search import NotAllowed
from collection_search.similarity_map import build_passage_map

from conftest import write

GARDEN = ("Tomatoes need compost rich soil and steady watering. Prune tomato side shoots weekly. "
          "Mulch the compost around the tomato plants to keep the soil moist.")
CARS = ("The engine needs fresh oil every season. Check the brake pads, tyre pressure and the gearbox. "
        "A worn clutch makes the gearbox grind when the engine is cold.")
TAXES = "File the tax return before April. Keep invoices and receipts for the accountant and the auditor."


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    write(str(root / "Notes" / "weekend.txt"), CARS + "\n\n" + GARDEN + "\n\n" + TAXES)
    write(str(root / "Notes" / "garden.txt"), GARDEN)
    write(str(root / "Notes" / "car.txt"), CARS)
    write(str(root / "Notes" / "unrelated.txt"), "Quantum chromodynamics lattice gauge symmetry gluon.")
    write(str(root / "Private" / "taxes.txt"), TAXES)
    return root


def collections(root):
    return [Collection("Notes", [str(root / "Notes")]),
            Collection("Private", [str(root / "Private")], active=False)]


def test_each_passage_links_to_the_matching_passage_elsewhere(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols, chunk_size=200, chunk_overlap=0)
    result = passage_links(store, str(docs / "Notes" / "weekend.txt"), cols, min_similarity=0.5)
    assert len(result.passages) == 3
    by_text = {p.text[:10]: p for p in result.passages}
    garden = by_text[GARDEN[:10]].links
    assert garden and os.path.basename(garden[0].path) == "garden.txt" and garden[0].same_text
    assert os.path.basename(by_text[CARS[:10]].links[0].path) == "car.txt"
    # The matching tax passage is in an unticked collection, so it is never linked.
    assert all("taxes.txt" not in link.path for p in result.passages for link in p.links)
    assert all("weekend.txt" not in link.path for p in result.passages for link in p.links)
    assert list(result.linked_paths())[0].endswith(("garden.txt", "car.txt"))


def test_links_respect_scope(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols, chunk_size=200, chunk_overlap=0)
    with pytest.raises(NotAllowed):
        passage_links(store, str(docs / "Private" / "taxes.txt"), cols)


def test_passage_map_neighbours_are_in_other_files(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols, chunk_size=200, chunk_overlap=0)
    m = build_passage_map(store, cols)
    assert m.passages and len(m) == m.total == 6  # 3 + 1 + 1 + 1; the unticked collection is left out
    assert len(m.locations) == len(m.texts) == len(m)
    for i in range(len(m)):
        for j in m.neighbours[i]:
            if j >= 0:
                assert m.paths[j] != m.paths[i]
    garden = [i for i, t in enumerate(m.texts) if t.startswith("Tomatoes")]
    assert len(garden) == 2 and garden[1] == m.neighbours[garden[0]][0]
    assert all(row[2] < 0.97 for row in m.strongest)  # near-copies aren't drawn as overview lines


def test_passage_map_samples_large_scopes(store, embedder, tmp_path):
    root = tmp_path / "many"
    for i in range(30):
        write(str(root / f"n{i}.txt"), "\n\n".join(f"Note {i} paragraph {k} about topic {k % 4}" for k in range(5)))
    cols = [Collection("Many", [str(root)])]
    sync(store, embedder, cols, chunk_size=40, chunk_overlap=0)
    m = build_passage_map(store, cols, limit=50)
    assert m.total > 50 and len(m) == 50
    assert np.isfinite(m.xy).all()


def test_engine_keeps_both_views(tmp_path, docs):
    from collection_search.embeddings import HashEmbedder
    from collection_search.engine import Engine
    from collection_search.settings import Settings

    settings = Settings.load(str(tmp_path / "s.json"))
    settings.preferences.watch_folders = False
    for c in collections(docs):
        settings.upsert_collection(c)
    engine = Engine(settings, str(tmp_path / "i.db"), lambda m: HashEmbedder())
    engine.start()
    try:
        end = time.time() + 20
        while engine.status().state != "idle" and time.time() < end:
            time.sleep(0.05)
        docs_map, passage_map = engine.similarity_map(), engine.similarity_map(passages=True)
        assert not docs_map.passages and passage_map.passages
        assert engine.similarity_map() is docs_map and engine.similarity_map(passages=True) is passage_map
        links = engine.passage_links(str(docs / "Notes" / "weekend.txt"))
        assert links.source.endswith("weekend.txt")
    finally:
        engine.stop()
