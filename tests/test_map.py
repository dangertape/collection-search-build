"""The similarity map: themes, layout, scope and the Map tab."""

import os
import time

import numpy as np
import pytest

from collection_search.collections import Collection
from collection_search.indexer import sync
from collection_search.similarity_map import Cancelled, build_map, theme_count

from conftest import write

TOPICS = {
    "garden": "tomatoes compost soil mulch watering prune seedlings greenhouse harvest",
    "cars": "engine gearbox clutch brake tyres oil exhaust mechanic garage",
    "taxes": "invoice receipts accountant return deduction allowance payroll ledger audit",
}


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    rng = np.random.default_rng(1)
    for topic, words in TOPICS.items():
        vocab = words.split()
        for i in range(12):
            text = " ".join(rng.choice(vocab, 40))
            write(str(root / "Notes" / topic / f"{topic}{i}.txt"), text)
    write(str(root / "Private" / "secret.txt"), "garden tomatoes compost soil")
    return root


def indexed(store, embedder, root):
    collections = [Collection("Notes", [str(root / "Notes")]),
                   Collection("Private", [str(root / "Private")], active=False)]
    sync(store, embedder, collections, chunk_size=400, chunk_overlap=0)
    return collections


def test_documents_about_the_same_thing_share_a_theme(store, embedder, docs):
    collections = indexed(store, embedder, docs)
    m = build_map(store, collections)
    assert len(m) == 36  # the unticked collection is left out
    assert not any("secret" in p for p in m.paths)
    assert len(m.themes) == theme_count(36) == 3
    for topic in TOPICS:
        themes = {int(m.theme[i]) for i, p in enumerate(m.paths) if os.path.basename(p).startswith(topic)}
        assert len(themes) == 1, topic
    named = {t.label.split(", ")[0] for t in m.themes}
    assert all(any(word in TOPICS[topic].split() + [topic] for topic in TOPICS) for word in named)
    assert {t.folder for t in m.themes} == set(TOPICS)
    assert np.isfinite(m.xy).all()
    # each document sits inside its theme's disc
    for i in range(len(m)):
        t = m.themes[int(m.theme[i])]
        assert np.hypot(m.xy[i, 0] - t.x, m.xy[i, 1] - t.y) <= t.radius + 1e-6
    # discs don't overlap
    for a in m.themes:
        for b in m.themes:
            if a.index < b.index:
                assert np.hypot(a.x - b.x, a.y - b.y) >= a.radius + b.radius
    assert sum(t.size for t in m.themes) == len(m)
    assert list(m.members(0))[0] in m.near(int(m.members(0)[0]), 5)


def test_same_index_draws_the_same_map(store, embedder, docs):
    collections = indexed(store, embedder, docs)
    a, b = build_map(store, collections), build_map(store, collections)
    assert np.array_equal(a.xy, b.xy) and [t.label for t in a.themes] == [t.label for t in b.themes]


def test_small_and_empty_scopes(store, embedder, tmp_path):
    root = tmp_path / "few"
    write(str(root / "one.txt"), "a single note about tomatoes")
    collection = [Collection("Few", [str(root)])]
    assert len(build_map(store, collection)) == 0  # nothing indexed yet
    sync(store, embedder, collection)
    m = build_map(store, collection)
    assert len(m) == 1 and len(m.themes) == 1 and m.themes[0].size == 1
    assert len(build_map(store, [])) == 0


def test_cancelled(store, embedder, docs):
    collections = indexed(store, embedder, docs)
    with pytest.raises(Cancelled):
        build_map(store, collections, cancelled=lambda: True)


def test_engine_reuses_the_map_until_something_changes(tmp_path, docs):
    from collection_search.embeddings import HashEmbedder
    from collection_search.engine import Engine
    from collection_search.settings import Settings

    settings = Settings.load(str(tmp_path / "s.json"))
    settings.preferences.watch_folders = False
    notes = Collection("Notes", [str(docs / "Notes")])
    settings.upsert_collection(notes)
    settings.upsert_collection(Collection("Private", [str(docs / "Private")]))
    engine = Engine(settings, str(tmp_path / "i.db"), lambda m: HashEmbedder())
    engine.start()
    try:
        end = time.time() + 20
        while engine.status().state != "idle" and time.time() < end:
            time.sleep(0.05)
        first = engine.similarity_map()
        assert len(first) == 37
        assert engine.similarity_map() is first
        settings.set_collection_active(notes.id, False)
        second = engine.similarity_map()
        assert second is not first and second.paths == [str(docs / "Private" / "secret.txt")]
    finally:
        engine.stop()
