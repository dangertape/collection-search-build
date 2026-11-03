import os
import time

import pytest

from collection_search.collections import Collection
from collection_search.embeddings import HashEmbedder
from collection_search.engine import Engine, NotReady
from collection_search.search import NotAllowed
from collection_search.settings import Settings

from conftest import write


def wait_for(predicate, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def setup(tmp_path):
    docs = tmp_path / "docs"
    write(str(docs / "a" / "lease.txt"), "The notice period is 90 days.")
    write(str(docs / "b" / "secret.txt"), "Private zebracorn notes.")
    settings = Settings.load(str(tmp_path / "settings.json"))
    settings.preferences.watch_folders = False
    a = Collection("A", [str(docs / "a")])
    b = Collection("B", [str(docs / "b")], active=False)
    settings.upsert_collection(a)
    settings.upsert_collection(b)
    models = []

    def factory(model):
        models.append(model)
        return HashEmbedder(64 if model == "small" else 256) if model != "small" else _Named(model)

    engine = Engine(settings, str(tmp_path / "index.db"), factory)
    yield engine, settings, docs, a, b, models
    engine.stop()


class _Named(HashEmbedder):
    def __init__(self, name):
        super().__init__(64)
        self.name = name


def test_not_ready_before_model_loads(setup):
    engine, *_ = setup
    with pytest.raises(NotReady):
        engine.search("x", 5)


def test_indexes_on_start_and_scopes_results(setup):
    engine, settings, docs, a, b, _ = setup
    engine.start()
    assert wait_for(lambda: engine.status().state == "idle")
    assert engine.status().files == 2  # inactive collections are indexed too, just not searchable
    assert [r.path for r in engine.search("notice period", 5)] == [str(docs / "a" / "lease.txt")]
    assert engine.search("zebracorn", 5) == [] or all("secret" not in r.path for r in engine.search("zebracorn", 5))
    with pytest.raises(NotAllowed):
        engine.read_file(str(docs / "b" / "secret.txt"))
    settings.set_collection_active(b.id, True)  # applies on the next call, no reindex
    assert any(r.path.endswith("secret.txt") for r in engine.search("zebracorn", 5))
    assert "zebracorn" in engine.read_file(str(docs / "b" / "secret.txt")).text


def test_folder_change_triggers_sync(setup, tmp_path):
    engine, settings, docs, a, *_ = setup
    engine.start()
    assert wait_for(lambda: engine.status().state == "idle")
    write(str(tmp_path / "new" / "fresh.md"), "Brand new quarterly report.")
    a.folders = a.folders + [str(tmp_path / "new")]
    settings.upsert_collection(a)
    assert wait_for(lambda: engine.status().files == 3 and engine.status().state == "idle")
    assert any(r.path.endswith("fresh.md") for r in engine.search("quarterly report", 5))


def test_model_change_rebuilds_index(setup):
    engine, settings, docs, *_rest, models = setup
    engine.start()
    assert wait_for(lambda: engine.status().state == "idle" and engine.status().files == 2)
    prefs = settings.preferences
    prefs.model = "small"
    settings.set_preferences(prefs)
    assert wait_for(lambda: models[-1:] == ["small"] and engine.status().state == "idle", 15)
    assert engine.status().files == 2
    assert engine.search("notice period", 5)[0].path.endswith("lease.txt")


def test_reindex_all(setup):
    engine, *_ = setup
    engine.start()
    assert wait_for(lambda: engine.status().state == "idle")
    first = engine.status().last_sync
    engine.reindex_all()
    assert wait_for(lambda: engine.status().last_sync != first and engine.status().state == "idle")
    assert engine.status().files == 2


def test_watcher_picks_up_new_file(tmp_path):
    docs = tmp_path / "docs"
    write(str(docs / "one.txt"), "first file")
    settings = Settings.load(str(tmp_path / "s.json"))
    settings.upsert_collection(Collection("A", [str(docs)]))
    engine = Engine(settings, str(tmp_path / "i.db"), lambda m: HashEmbedder())
    engine.start()
    try:
        assert wait_for(lambda: engine.status().files == 1)
        write(str(docs / "two.txt"), "second file appears")
        assert wait_for(lambda: engine.status().files == 2, 15)
    finally:
        engine.stop()
