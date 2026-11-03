import json
from datetime import date

from collection_search.collections import Collection
from collection_search.settings import INDEX, SCOPE, TOOLS, Preferences, Settings


def test_roundtrip(tmp_path):
    path = str(tmp_path / "settings.json")
    s = Settings.load(path)
    c = Collection("Client work", ["/docs/clients"], excluded=["/docs/clients/archive"], extensions=[".pdf"],
                   modified_after=date(2025, 1, 1), exclude_patterns=["*draft*"])
    s.upsert_collection(c)
    s.set_tool_enabled("read_file", False)
    loaded = Settings.load(path)
    [back] = loaded.collections()
    assert (back.id, back.name, back.folders, back.excluded, back.extensions) == (
        c.id, "Client work", ["/docs/clients"], ["/docs/clients/archive"], [".pdf"])
    assert back.modified_after == date(2025, 1, 1) and back.exclude_patterns == ["*draft*"]
    assert not loaded.tool_enabled("read_file")
    assert loaded.tool_enabled("search")  # unknown tools default on


def test_change_kinds(tmp_path):
    s = Settings.load(str(tmp_path / "s.json"))
    seen = []
    s.subscribe(seen.append)
    c = Collection("A", [str(tmp_path)])
    s.upsert_collection(c)
    assert seen == [INDEX, SCOPE]
    seen.clear()
    c.exclude_patterns = ["*.tmp"]  # search-time filter: no reindex
    s.upsert_collection(c)
    assert seen == [SCOPE]
    seen.clear()
    c.folders = [str(tmp_path / "other")]
    s.upsert_collection(c)
    assert seen == [INDEX, SCOPE]
    seen.clear()
    s.set_collection_active(c.id, False)
    s.set_collection_active(c.id, False)  # no-op
    s.set_tool_enabled("search", False)
    assert seen == [SCOPE, TOOLS]


def test_readers_get_copies(tmp_path):
    s = Settings.load(str(tmp_path / "s.json"))
    s.upsert_collection(Collection("A", [str(tmp_path)]))
    s.collections()[0].active = False
    assert s.active_collections()[0].name == "A"


def test_broken_file_is_set_aside(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    s = Settings.load(str(path))
    assert s.collections() == []
    assert (tmp_path / "settings.json.broken").exists()
    assert json.loads(path.read_text())["version"] == 1


def test_unknown_preferences_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"preferences": {"chunk_size": 1000, "from_the_future": 1}}))
    assert Settings.load(str(path)).preferences == Preferences(chunk_size=1000)
