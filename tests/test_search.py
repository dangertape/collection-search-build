"""Search behaviour, above all scope enforcement: nothing from an inactive
or non-matching collection may ever be returned."""

import os
import time
from datetime import date

import pytest

from collection_search.collections import Collection
from collection_search.indexer import sync
from collection_search.search import search
from collection_search.store import ModelMismatch, Store

from conftest import make_docx, make_pdf, write

SECRET = "zebracorn"  # appears only in files that must stay hidden


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "docs"
    clients = root / "Clients"
    personal = root / "Personal"
    write(str(clients / "acme_lease.txt"), "The Acme lease has a notice period of 90 days.\n\nInvoice 4471-B is overdue.")
    write(str(clients / "Archive" / "old.txt"), f"Archived {SECRET} lease from 2019.")
    write(str(clients / "acme_draft.txt"), f"Draft lease {SECRET} terms.")
    make_docx(str(clients / "beta_contract.docx"), ["Beta contract covers software support."], [["Fee", "1200 EUR"]])
    make_pdf(str(clients / "gamma.pdf"), ["Gamma report on quarterly revenue"])
    write(str(personal / "diary.md"), f"My private {SECRET} diary entry about the lease.")
    return root


def collections(root, personal_active=False, **client_kwargs):
    clients = Collection(
        "Client work",
        [str(root / "Clients")],
        excluded=[str(root / "Clients" / "Archive")],
        exclude_patterns=["*draft*"],
        **client_kwargs,
    )
    personal = Collection("Personal", [str(root / "Personal")], active=personal_active)
    return [clients, personal]


def paths(results):
    return {os.path.basename(r.path) for r in results}


def test_finds_documents_of_every_type(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    assert "acme_lease.txt" in paths(search(store, embedder, "notice period", cols))
    assert "beta_contract.docx" in paths(search(store, embedder, "software support fee", cols))
    assert "gamma.pdf" in paths(search(store, embedder, "quarterly revenue", cols))


def test_result_fields(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    top = search(store, embedder, "Gamma quarterly revenue", cols, limit=1)[0]
    assert top.path.endswith("gamma.pdf")
    assert top.location == "page 1"
    assert top.collections == ("Client work",)
    assert "revenue" in top.text


def test_inactive_collection_never_returned(store, embedder, docs):
    cols = collections(docs, personal_active=False)
    sync(store, embedder, cols)  # Personal is indexed even though it is off
    results = search(store, embedder, f"{SECRET} diary", cols, limit=50)
    assert "diary.md" not in paths(results)
    assert all(SECRET not in r.text for r in results)


def test_toggling_collection_needs_no_reindex(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    cols[1].active = True
    assert "diary.md" in paths(search(store, embedder, f"{SECRET} diary", cols))


def test_excluded_folder_and_pattern_never_returned(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    results = search(store, embedder, f"{SECRET} lease", cols, limit=50)
    assert not {"old.txt", "acme_draft.txt"} & paths(results)
    assert all(SECRET not in r.text for r in results)


def test_pattern_edit_applies_without_reindex(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    cols[0].exclude_patterns = []
    assert "acme_draft.txt" in paths(search(store, embedder, f"{SECRET} draft", cols))


def test_date_filter_applies_at_search_time(store, embedder, docs):
    old = time.mktime((2020, 1, 1, 0, 0, 0, 0, 0, -1))
    os.utime(docs / "Clients" / "gamma.pdf", (old, old))
    cols = collections(docs)
    sync(store, embedder, cols)
    assert "gamma.pdf" in paths(search(store, embedder, "quarterly revenue", cols))
    cols[0].modified_after = date(2025, 1, 1)
    assert "gamma.pdf" not in paths(search(store, embedder, "quarterly revenue", cols, limit=50))


def test_no_active_collections_returns_nothing(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    cols[0].active = False
    assert search(store, embedder, "lease", cols) == []


def test_file_in_two_collections_indexed_once_and_named_by_both(store, embedder, docs):
    cols = collections(docs) + [Collection("Leases", [str(docs / "Clients")], include_patterns=["*lease*"])]
    sync(store, embedder, cols)
    # Client files (draft included: patterns are checked at search time), diary, and the
    # archive file, which "Leases" covers because it has no exclusion. Each stored once.
    assert store.stats()["files"] == 6
    hit = next(r for r in search(store, embedder, "notice period", cols) if r.path.endswith("acme_lease.txt"))
    assert set(hit.collections) == {"Client work", "Leases"}


def test_keyword_search_finds_exact_codes(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    top = search(store, embedder, "4471-B", cols, limit=1)[0]
    assert top.path.endswith("acme_lease.txt")


def test_fts_syntax_in_query_is_harmless(store, embedder, docs):
    cols = collections(docs)
    sync(store, embedder, cols)
    for q in ['"unbalanced', "lease AND OR NOT", "col:value*", "(", "  "]:
        search(store, embedder, q, cols)  # must not raise


def test_sync_picks_up_changes_and_removals(store, embedder, docs):
    cols = collections(docs)
    first = sync(store, embedder, cols)
    assert first.added == 5 and first.errors == []
    assert sync(store, embedder, cols).unchanged == 5

    lease = docs / "Clients" / "acme_lease.txt"
    lease.write_text("Completely rewritten: the parking clause.", encoding="utf-8")
    os.utime(lease, (time.time() + 5, time.time() + 5))
    os.remove(docs / "Clients" / "gamma.pdf")
    result = sync(store, embedder, cols)
    assert (result.updated, result.removed) == (1, 1)
    assert "gamma.pdf" not in paths(search(store, embedder, "quarterly revenue", cols, limit=50))
    assert search(store, embedder, "parking clause", cols, limit=1)[0].path.endswith("acme_lease.txt")


def test_unreadable_file_is_recorded_not_fatal(store, embedder, docs):
    (docs / "Clients" / "broken.pdf").write_bytes(b"garbage")
    cols = collections(docs)
    result = sync(store, embedder, cols)
    assert [os.path.basename(p) for p, _ in result.errors] == ["broken.pdf"]
    assert store.stats()["files_with_errors"] == 1


def test_index_refuses_a_different_model(tmp_path, embedder):
    path = str(tmp_path / "i.db")
    Store(path, embedder.name, embedder.dim).close()
    with pytest.raises(ModelMismatch):
        Store(path, "other-model", 384)


def test_folder_walk_keys_match_normalised_paths(tmp_path):
    from collection_search.indexer import files_for
    from collection_search.store import path_key

    root = tmp_path / "docs"
    write(str(root / "A" / "one.md"), "x")
    write(str(root / "A" / "Skip" / "two.md"), "x")
    write(str(root / "three.txt"), "x")
    write(str(root / "image.png"), "x")
    col = Collection("C", [str(root)], excluded=[str(root / "A" / "Skip")])
    found = files_for([col])
    assert set(found) == {path_key(str(root / "A" / "one.md")), path_key(str(root / "three.txt"))}
    counts = []
    files_for([col], scanning=counts.append)
    assert files_for([col], cancelled=lambda: True) == {}
