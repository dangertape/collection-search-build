"""On/off switches on folders and documents inside a collection.

An item that is switched off must drop out of everything Claude and the
window can see (search, reading, similar documents, links, the map), while
staying in the index so switching it back on is instant.
"""

import json
import os

import pytest

from collection_search.collections import Collection
from collection_search.indexer import sync
from collection_search.search import NotAllowed, allowed_files, find_similar, read_file, search
from collection_search.settings import SCOPE, Settings
from collection_search.store import path_key

from conftest import write

SECRET = "zebracorn"


@pytest.fixture
def docs(tmp_path):
    root = tmp_path / "Bible"
    write(str(root / "Exodus" / "Exod-20.md"), f"Exodus twenty commandments {SECRET} tablets.")
    write(str(root / "Exodus" / "Exod-21.md"), f"Exodus twenty-one ordinances {SECRET} servants.")
    write(str(root / "Exodus" / "Notes" / "idols.md"), f"Notes on idols and {SECRET} images.")
    write(str(root / "Genesis" / "Gen-1.md"), f"Genesis creation {SECRET} light.")
    return root


def k(path) -> str:
    return path_key(str(path))


def names(results):
    return {os.path.basename(r.path) for r in results}


# ---- the switch rules ----------------------------------------------------------------
def test_everything_is_on_by_default(docs):
    c = Collection("Bible", [str(docs)])
    assert c.switched_on(k(docs / "Exodus" / "Exod-20.md"))
    assert c.switch_state(k(docs)) == "on" and not c.has_switched_off()


def test_nearest_switch_wins(docs):
    c = Collection("Bible", [str(docs)], switches={str(docs / "Exodus"): False,
                                                   str(docs / "Exodus" / "Notes"): True})
    assert not c.switched_on(k(docs / "Exodus" / "Exod-20.md"))
    assert c.switched_on(k(docs / "Exodus" / "Notes" / "idols.md"))
    assert c.switched_on(k(docs / "Genesis" / "Gen-1.md"))


def test_states_on_mixed_off(docs):
    c = Collection("Bible", [str(docs)])
    c = Collection("Bible", [str(docs)], switches=c.with_switch(str(docs / "Exodus" / "Exod-21.md"), False))
    assert c.switch_state(k(docs / "Exodus" / "Exod-21.md")) == "off"
    assert c.switch_state(k(docs / "Exodus" / "Exod-20.md")) == "on"
    assert c.switch_state(k(docs / "Exodus")) == "mixed"
    assert c.switch_state(k(docs)) == "mixed"
    assert c.switch_state(k(docs / "Genesis")) == "on"
    assert c.has_switched_off()


def test_switch_inside_an_off_folder_makes_it_mixed(docs):
    c = Collection("Bible", [str(docs)])
    c = Collection("Bible", [str(docs)], switches=c.with_switch(str(docs / "Exodus"), False))
    c = Collection("Bible", [str(docs)], switches=c.with_switch(str(docs / "Exodus" / "Exod-20.md"), True))
    assert c.switch_state(k(docs / "Exodus")) == "mixed"
    assert c.switched_on(k(docs / "Exodus" / "Exod-20.md"))
    assert not c.switched_on(k(docs / "Exodus" / "Exod-21.md"))


def test_switching_a_folder_clears_switches_inside_it(docs):
    c = Collection("Bible", [str(docs)])
    c = Collection("Bible", [str(docs)], switches=c.with_switch(str(docs / "Exodus" / "Exod-21.md"), False))
    c = Collection("Bible", [str(docs)], switches=c.with_switch(str(docs / "Exodus" / "Notes"), False))
    # a mixed folder clicked turns fully on, and the saved list is empty again
    switches = c.with_switch(str(docs / "Exodus"), True)
    assert switches == {}
    # switching off a folder replaces the switches inside it with one
    switches = c.with_switch(str(docs / "Exodus"), False)
    assert switches == {str(docs / "Exodus"): False}


def test_redundant_switch_is_not_saved(docs):
    c = Collection("Bible", [str(docs)], switches={str(docs / "Exodus"): False})
    # turning on a document in an on folder, or off one in an off folder, says nothing new
    assert c.with_switch(str(docs / "Genesis" / "Gen-1.md"), True) == c.switches
    assert c.with_switch(str(docs / "Exodus" / "Exod-20.md"), False) == c.switches


# ---- saved settings ----------------------------------------------------------------------
def test_switches_are_saved_and_reloaded(tmp_path, docs):
    path = str(tmp_path / "settings.json")
    s = Settings.load(path)
    c = Collection("Bible", [str(docs)])
    s.upsert_collection(c)
    seen = []
    s.subscribe(seen.append)
    s.set_switch(c.id, str(docs / "Exodus"), False)
    s.set_switch(c.id, str(docs / "Exodus"), False)  # no change, no event
    assert seen == [SCOPE]  # search-time only: nothing is re-indexed
    assert json.load(open(path))["collections"][0]["switches"] == {str(docs / "Exodus"): False}
    [back] = Settings.load(path).collections()
    assert back.switches == {str(docs / "Exodus"): False}
    assert not back.switched_on(k(docs / "Exodus" / "Exod-20.md"))


def test_old_settings_without_switches_load(tmp_path, docs):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 2, "collections": [{"name": "Bible", "folders": [str(docs)]}]}))
    [c] = Settings.load(str(path)).collections()
    assert c.switches == {} and c.switched_on(k(docs / "Genesis" / "Gen-1.md"))


def test_editing_a_collection_keeps_its_switches(tmp_path, docs):
    s = Settings.load(str(tmp_path / "s.json"))
    c = Collection("Bible", [str(docs)])
    s.upsert_collection(c)
    s.set_switch(c.id, str(docs / "Genesis"), False)
    edited = s.get_collection(c.id)
    edited.exclude_patterns = ["*draft*"]
    s.upsert_collection(edited)
    assert s.get_collection(c.id).switches == {str(docs / "Genesis"): False}


# ---- what search sees --------------------------------------------------------------------
def test_off_items_drop_out_of_search_read_similar_and_map(store, embedder, docs):
    c = Collection("Bible", [str(docs)])
    sync(store, embedder, [c])
    everything = names(search(store, embedder, SECRET, [c], limit=20))
    assert everything == {"Exod-20.md", "Exod-21.md", "idols.md", "Gen-1.md"}

    switches = c.with_switch(str(docs / "Exodus"), False)
    c = Collection("Bible", [str(docs)], switches=switches, id=c.id)
    c = Collection("Bible", [str(docs)], switches=c.with_switch(str(docs / "Exodus" / "Notes"), True), id=c.id)
    assert names(search(store, embedder, SECRET, [c], limit=20)) == {"idols.md", "Gen-1.md"}

    with pytest.raises(NotAllowed):
        read_file(store, str(docs / "Exodus" / "Exod-20.md"), [c])
    assert SECRET in read_file(store, str(docs / "Genesis" / "Gen-1.md"), [c]).text

    similar = find_similar(store, str(docs / "Genesis" / "Gen-1.md"), [c], limit=10)
    assert {os.path.basename(r.path) for r in similar.results} <= {"idols.md"}

    allowed = allowed_files(store, [c])  # what the Map draws
    files = {rec.id: os.path.basename(rec.path) for rec in store.files().values()}
    assert {files[i] for i in allowed} == {"idols.md", "Gen-1.md"}

    # still indexed: switching back on needs no re-index
    assert len(store.files()) == 4
    c = Collection("Bible", [str(docs)], id=c.id)
    assert names(search(store, embedder, SECRET, [c], limit=20)) == everything


def test_switch_only_affects_its_own_collection(store, embedder, docs):
    a = Collection("A", [str(docs)], switches={str(docs / "Genesis"): False})
    b = Collection("B", [str(docs / "Genesis")])
    sync(store, embedder, [a, b])
    results = search(store, embedder, "Genesis creation light", [a, b], limit=20)
    gen = next(r for r in results if r.path.endswith("Gen-1.md"))
    assert gen.collections == ("B",)
