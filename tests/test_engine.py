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


class SlowEmbedder(HashEmbedder):
    def embed_documents(self, texts):
        time.sleep(0.05)
        return super().embed_documents(texts)


def test_file_changes_dont_restart_a_running_scan(tmp_path):
    """Constant change events (Obsidian, sync apps) must not keep a scan from finishing."""
    import threading

    docs = tmp_path / "docs"
    for i in range(20):
        write(str(docs / f"n{i}.md"), f"Note number {i}.")
    settings = Settings.load(str(tmp_path / "s.json"))
    settings.preferences.watch_folders = False
    settings.upsert_collection(Collection("A", [str(docs)]))
    engine = Engine(settings, str(tmp_path / "i.db"), lambda m: SlowEmbedder())
    stop = threading.Event()

    def ping():
        while not stop.is_set():
            engine.request_sync()
            time.sleep(0.02)

    engine.start()
    pinger = threading.Thread(target=ping, daemon=True)
    pinger.start()
    try:
        assert wait_for(lambda: engine.status().state == "idle" and engine.status().files == 20, 15)
    finally:
        stop.set()
        engine.stop()


def test_watcher_ignores_files_it_does_not_index(tmp_path):
    from types import SimpleNamespace

    from collection_search.watcher import FolderWatcher

    docs = str(tmp_path / "vault")
    settings = Settings.load(str(tmp_path / "s.json"))
    settings.upsert_collection(Collection("A", [docs], extensions=[".md"]))
    engine = Engine(settings, str(tmp_path / "i.db"), lambda m: HashEmbedder())
    watcher = FolderWatcher(lambda: None, engine._wanted)

    def event(kind, path, is_dir=False, dest=""):
        return SimpleNamespace(event_type=kind, src_path=path, dest_path=dest, is_directory=is_dir)

    join = os.path.join
    assert watcher.relevant(event("modified", join(docs, "note.md")))
    assert watcher.relevant(event("moved", join(docs, "x.tmp"), dest=join(docs, "note.md")))  # safe-save
    assert not watcher.relevant(event("modified", join(docs, ".obsidian", "workspace.json")))
    assert not watcher.relevant(event("modified", join(docs, "photo.png")))
    assert not watcher.relevant(event("modified", join(str(tmp_path), "elsewhere.md")))
    assert not watcher.relevant(event("created", join(docs, ".obsidian", "plugins"), is_dir=True))
    assert not watcher.relevant(event("modified", join(docs, "sub"), is_dir=True))
    assert watcher.relevant(event("deleted", join(docs, "sub"), is_dir=True))
