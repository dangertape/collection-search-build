"""Finding documents like a given one: document vectors, passage evidence, and scope."""

import os

import numpy as np
import pytest

from collection_search.collections import Collection
from collection_search.indexer import sync
from collection_search.search import NotAllowed, NothingToCompare, find_similar
from collection_search.store import Store, document_vector

from conftest import write

GARDEN = ("Tomatoes need compost rich soil and steady watering. Prune tomato side shoots weekly. "
          "Mulch the compost around the tomato plants to keep the soil moist.")
CARS = ("The engine needs fresh oil every season. Check the brake pads, tyre pressure and the gearbox. "
        "A worn clutch makes the gearbox grind when the engine is cold. ")


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    write(str(root / "Notes" / "garden.txt"), GARDEN + "\n\n" + "Harvest tomatoes when they turn red.")
    write(str(root / "Notes" / "tomatoes.txt"), "Tomato plants like compost, mulch and moist soil. Water tomatoes "
          "often and prune the side shoots.")
    write(str(root / "Notes" / "taxes.txt"), "File the tax return before April. Keep invoices and receipts "
          "for the accountant.")
    # Mostly about cars, with one paragraph that matches the garden note closely.
    write(str(root / "Notes" / "weekend.txt"), (CARS * 4) + "\n\n" + GARDEN + "\n\n" + (CARS * 4))
    write(str(root / "Notes" / "empty.txt"), "")
    write(str(root / "Private" / "garden_copy.txt"), GARDEN)
    return root


def cols(root, private_active=False):
    return [Collection("Notes", [str(root / "Notes")]),
            Collection("Private", [str(root / "Private")], active=private_active)]


def names(result):
    return [os.path.basename(r.path) for r in result.results]


def test_most_similar_first_and_source_left_out(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c, chunk_size=200, chunk_overlap=0)
    result = find_similar(store, str(docs / "Notes" / "garden.txt"), c)
    found = names(result)
    assert found[0] == "tomatoes.txt"
    assert "garden.txt" not in found
    assert found.index("tomatoes.txt") < found.index("taxes.txt")
    assert result.source_collections == ("Notes",)
    top = result.results[0]
    assert top.collections == ("Notes",) and 0 < top.score <= 1
    assert top.passages and top.passages[0].source_location


def test_shared_passage_is_found_and_shown(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c, chunk_size=200, chunk_overlap=0)
    result = find_similar(store, str(docs / "Notes" / "garden.txt"), c, passages=1)
    weekend = next(r for r in result.results if r.path.endswith("weekend.txt"))
    assert weekend.passage_similarity > weekend.document_similarity
    assert "compost" in weekend.passages[0].text.lower()
    taxes = next(r for r in result.results if r.path.endswith("taxes.txt"))
    assert weekend.score > taxes.score


def test_inactive_collections_never_returned(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c, chunk_size=200, chunk_overlap=0)
    assert "garden_copy.txt" not in names(find_similar(store, str(docs / "Notes" / "garden.txt"), c))
    assert "garden_copy.txt" in names(find_similar(store, str(docs / "Notes" / "garden.txt"), cols(docs, True)))


def test_source_must_be_in_an_active_collection(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c)
    for path in [str(docs / "Private" / "garden_copy.txt"), str(docs / "Notes" / "missing.txt"), "/etc/passwd"]:
        with pytest.raises(NotAllowed):
            find_similar(store, path, c)


def test_empty_source(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c)
    with pytest.raises(NothingToCompare):
        find_similar(store, str(docs / "Notes" / "empty.txt"), c)


def test_limit_and_passage_count(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c, chunk_size=200, chunk_overlap=0)
    result = find_similar(store, str(docs / "Notes" / "garden.txt"), c, limit=2, passages=0)
    assert len(result.results) == 2 and all(r.passages == () for r in result.results)


def test_document_vector_is_unit_mean():
    v = document_vector(np.array([[1, 0], [0, 2]], dtype=np.float32))
    assert np.allclose(v, [2 ** -0.5, 2 ** -0.5])
    assert document_vector(np.zeros((0, 2), dtype=np.float32)) is None


def test_old_index_gets_document_vectors(tmp_path, embedder, docs):
    path = str(tmp_path / "old.db")
    s = Store(path, embedder.name, embedder.dim)
    sync(s, embedder, cols(docs))
    s.db.execute("DELETE FROM doc_vectors")
    s.db.commit()
    s.close()
    s = Store(path, embedder.name, embedder.dim)
    try:
        with_chunks = s.db.execute("SELECT COUNT(DISTINCT file_id) FROM chunks").fetchone()[0]
        assert s.db.execute("SELECT COUNT(*) FROM doc_vectors").fetchone()[0] == with_chunks > 0
    finally:
        s.close()


def test_removed_file_drops_its_document_vector(store, embedder, docs):
    c = cols(docs)
    sync(store, embedder, c)
    before = store.db.execute("SELECT COUNT(*) FROM doc_vectors").fetchone()[0]
    os.remove(docs / "Notes" / "taxes.txt")
    sync(store, embedder, c)
    assert store.db.execute("SELECT COUNT(*) FROM doc_vectors").fetchone()[0] == before - 1
