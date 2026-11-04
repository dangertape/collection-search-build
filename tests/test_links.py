"""Headings, links, verse references, paging, reading around a passage, and explore."""

import json
import os
import sqlite3

import pytest

from collection_search import claude_config
from collection_search.collections import Collection
from collection_search.indexer import sync
from collection_search.links import chapter_of, extract, verse_references
from collection_search.parsers import Section
from collection_search.search import (
    LocationNotFound, NotAllowed, document_links, explore, read_file, search, search_page,
)
from collection_search.settings import Settings
from collection_search.store import Store

from conftest import write

SECRET = "zebracorn"


# ---- parsing -------------------------------------------------------------------------

def test_verse_references():
    text = ("See Exod 20:4-6, 1 Cor. 8:4 and John 3:16; Ps 115:4–8, I Jn 2:2, Isa 44:9. "
            "The meeting is 3:30 and the ratio is 2:1.")
    labels = [label for _, _, label in verse_references(text)]
    assert labels == ["Exodus 20:4-6", "1 Corinthians 8:4", "John 3:16", "Psalms 115:4-8",
                      "1 John 2:2", "Isaiah 44:9"]


def test_chapter_files():
    assert chapter_of("Exod-20.md") == ("Exodus", 20)
    assert chapter_of("/x/02 - Exodus/Exodus 20.md") == ("Exodus", 20)
    assert chapter_of("1 Cor-13.md") == ("1 Corinthians", 13)
    assert chapter_of("1John-1.md") == ("1 John", 1)
    assert chapter_of("Idols study.md") is None


def test_extract_links(tmp_path):
    src = str(tmp_path / "notes" / "idols.md")
    sections = [Section("Compare [[Exod-20#v4|the second commandment]] and [the cross](../Cross.md#top), "
                        "[web](https://example.com), Isa 44:9.", "paragraph 3", "Idols > OT")]
    links = extract(src, sections)
    assert [(l.kind, l.label) for l in links] == [
        ("link", "the second commandment"), ("link", "the cross"), ("verse", "Isaiah 44:9")]
    assert links[0].target == "name:exod-20"
    assert links[1].target.startswith("path:") and links[1].target.endswith("cross.md".lower()) or \
        links[1].target.endswith("Cross.md")
    assert links[2].target == "verse:Isaiah|44" and links[2].heading == "Idols > OT"


# ---- a small Bible study kit -------------------------------------------------------------

EXOD_20 = """# Exodus 20

###### 1
God spoke all these words, saying,

###### 2
I am Yahweh your God, who brought you out of the land of Egypt.

###### 3
You shall have no other gods before me.

###### 4
You shall not make for yourselves an idol, nor any image of anything that is in the heavens above.

###### 5
You shall not bow yourself down to them, nor serve them.
"""

ISA_44 = """# Isaiah 44

###### 9
Everyone who makes a carved image is vain. The things that they delight in will not profit.

###### 10
Who has fashioned a god, or molds an image that is profitable for nothing?
"""

IDOLS = """# Idols and the cross

## Old Testament

The second commandment ([[Exod-20]]) forbids images. Isaiah mocks idol makers in Isa 44:9-10.

## New Testament

Paul says an idol is nothing (1 Cor 8:4). See also [[Cross notes]].
"""

CROSS = """# Cross notes

The cross is where the true God is shown. Compare Exodus 20:3 with Philippians 2.
"""


@pytest.fixture
def kit(tmp_path, store, embedder):
    root = tmp_path / "Bible Study Kit"
    write(str(root / "Scripture" / "02 - Exodus" / "Exod-20.md"), EXOD_20)
    write(str(root / "Scripture" / "23 - Isaiah" / "Isa-44.md"), ISA_44)
    write(str(root / "Notes" / "Idols.md"), IDOLS)
    write(str(root / "Notes" / "Cross notes.md"), CROSS)
    write(str(tmp_path / "Private" / "diary.md"), f"My {SECRET} diary. [[Exod-20]] Exodus 20:4 [[Idols]]")
    kit = Collection("Bible Study Kit", [str(root)])
    private = Collection("Private", [str(tmp_path / "Private")], active=False)
    cols = [kit, private]
    sync(store, embedder, cols)
    return root, cols


def test_results_carry_headings(store, embedder, kit):
    root, cols = kit
    results = search(store, embedder, "make an idol image", cols, limit=10)
    exod = [r for r in results if r.path.endswith("Exod-20.md")]
    assert exod and exod[0].heading.startswith("Exodus 20")


def test_paging_and_one_file_cannot_fill_a_page(store, embedder, tmp_path):
    for i in range(6):
        write(str(tmp_path / "d" / "big.txt"), "\n\n".join(f"Alpha topic paragraph {n}. " * 30 for n in range(12)))
        write(str(tmp_path / "d" / f"other{i}.txt"), f"Alpha topic, a different note number {i}.")
    cols = [Collection("D", [str(tmp_path / "d")])]
    sync(store, embedder, cols, chunk_size=200, chunk_overlap=20)
    page, more = search_page(store, embedder, "alpha topic", cols, limit=6)
    assert sum(r.path.endswith("big.txt") for r in page) <= 3
    assert more
    nxt, _ = search_page(store, embedder, "alpha topic", cols, limit=6, offset=6)
    seen = {(r.path, r.text) for r in page}
    assert nxt and not seen & {(r.path, r.text) for r in nxt}


def test_read_around_location_and_heading(store, kit):
    root, cols = kit
    path = str(root / "Scripture" / "02 - Exodus" / "Exod-20.md")
    part = read_file(store, path, cols, around="Exodus 20 > 4", context=0)
    assert "idol" in part.text and "God spoke" not in part.text and part.shown
    assert "[paragraph 5 | Exodus 20 > 4]" in part.text
    part = read_file(store, path, cols, around="paragraphs 3-4", context=0)
    assert "Yahweh" in part.text and "other gods" in part.text and "idol" not in part.text
    part = read_file(store, path, cols, around="Exodus 20 > 3-4", context=0)
    assert "other gods" in part.text and "idol" in part.text and "bow" not in part.text
    with pytest.raises(LocationNotFound):
        read_file(store, path, cols, around="Leviticus 3")
    assert not read_file(store, path, cols).shown


def test_document_links(store, kit):
    root, cols = kit
    idols = str(root / "Notes" / "Idols.md")
    links = document_links(store, idols, cols)
    out = {os.path.basename(l["path"]) for l in links["links_to"]}
    assert out == {"Exod-20.md", "Isa-44.md", "Cross notes.md"}
    assert "1 Corinthians 8:4" in links["verses_mentioned"]
    assert "1 Corinthians 8:4" in links["links_not_found"]  # no chapter file for it in the kit
    back = {os.path.basename(l["path"]) for l in links["linked_from"]}
    assert back == set()  # only the hidden diary links to Idols

    exod = document_links(store, str(root / "Scripture" / "02 - Exodus" / "Exod-20.md"), cols)
    back = {os.path.basename(l["path"]): l for l in exod["linked_from"]}
    assert set(back) == {"Idols.md", "Cross notes.md"}  # by [[link]] and by "Exodus 20:3"
    assert back["Idols.md"]["examples"][0]["heading"] == "Idols and the cross > Old Testament"
    assert all(SECRET not in json.dumps(l) and "diary" not in l["path"] for l in exod["linked_from"])


def test_links_respect_scope(store, kit):
    root, cols = kit
    with pytest.raises(NotAllowed):
        document_links(store, str(root.parent / "Private" / "diary.md"), cols)
    cols[1].active = True
    exod = document_links(store, str(root / "Scripture" / "02 - Exodus" / "Exod-20.md"), cols)
    assert "diary.md" in {os.path.basename(l["path"]) for l in exod["linked_from"]}


def test_explore_follows_links(store, embedder, kit):
    root, cols = kit
    result = explore(store, embedder, cols, path=str(root / "Notes" / "Cross notes.md"), steps=2)
    found = {os.path.basename(d["path"]): d for d in result["documents"]}
    assert found["Cross notes.md"]["step"] == 0
    assert {"Exod-20.md", "Idols.md"} <= set(found)  # Exod-20 by verse reference, Idols links to Cross
    assert "Isa-44.md" in found  # two steps: Cross -> Idols -> Isa-44
    assert "diary.md" not in found
    assert any(c["how"] == "mentions verses in" for c in result["connections"])

    by_query = explore(store, embedder, cols, query="idol image", steps=1, max_documents=3)
    assert len(by_query["documents"]) <= 3
    assert all("relevance" in d and d["passage"]["text"] for d in by_query["documents"])


# ---- upgrades ---------------------------------------------------------------------------

def test_old_index_is_emptied_for_reindex(tmp_path, embedder):
    path = str(tmp_path / "index.db")
    s = Store(path, embedder.name, embedder.dim)
    write(str(tmp_path / "d" / "a.txt"), "hello")
    sync(s, embedder, [Collection("D", [str(tmp_path / "d")])])
    s.db.execute("UPDATE meta SET value = '1' WHERE key = 'schema'")
    s.db.commit()
    s.close()
    s = Store(path, embedder.name, embedder.dim)
    assert s.stats()["files"] == 0
    cols = [c[1] for c in s.db.execute("PRAGMA table_info(chunks)")]
    assert "heading" in cols
    s.close()


def test_settings_upgrade(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 1, "collections": [], "tools": {"read_file": False, "search": True},
                                "preferences": {"default_result_count": 8, "max_result_count": 25}}))
    s = Settings.load(str(path))
    assert not s.tool_enabled("read_document") and s.tool_enabled("search_documents")
    assert (s.preferences.default_result_count, s.preferences.max_result_count) == (15, 50)

    path.write_text(json.dumps({"version": 1, "preferences": {"default_result_count": 5, "max_result_count": 25}}))
    s = Settings.load(str(path))
    assert s.preferences.default_result_count == 5  # a value the user chose is kept


def test_connecting_replaces_old_name(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"collection-search": {"command": "old"}, "fal": {"command": "f"}}}))
    assert claude_config.has_old_entry(str(path))
    claude_config.install(str(path))
    servers = json.loads(path.read_text())["mcpServers"]
    assert set(servers) == {"my-documents", "fal"}
    assert not claude_config.has_old_entry(str(path))
