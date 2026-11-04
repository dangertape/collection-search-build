"""A corrupt index file is set aside and rebuilt instead of breaking search."""

import os

from collection_search.collections import Collection
from collection_search.embeddings import HashEmbedder
from collection_search.engine import Engine, NotReady
from collection_search.settings import Settings

from conftest import write
from test_engine import wait_for


def _engine(tmp_path):
    docs = tmp_path / "docs"
    write(str(docs / "lease.txt"), "The notice period is 90 days.")
    settings = Settings.load(str(tmp_path / "settings.json"))
    settings.preferences.watch_folders = False
    settings.upsert_collection(Collection("A", [str(docs)]))
    return Engine(settings, str(tmp_path / "index.db"), lambda m: HashEmbedder())


def _corrupt(path):
    with open(path, "r+b") as f:
        f.seek(4096)  # keep the header, scramble the first pages
        f.write(os.urandom(4096 * 3))


def test_corrupt_index_at_start_is_rebuilt(tmp_path):
    engine = _engine(tmp_path)
    engine.start()
    assert wait_for(lambda: engine.status().state == "idle")
    engine.stop()
    db = str(tmp_path / "index.db")
    _corrupt(db)

    engine = _engine(tmp_path)
    engine.start()
    try:
        assert wait_for(lambda: engine.status().state == "idle" and engine.status().files == 1)
        assert os.path.exists(db + ".damaged")
        assert engine.search("notice period", 3)
    finally:
        engine.stop()


def test_corruption_found_while_running_is_rebuilt(tmp_path):
    engine = _engine(tmp_path)
    engine.start()
    try:
        assert wait_for(lambda: engine.status().state == "idle")
        assert engine.search("notice period", 3)
        import sqlite3

        # A query that hits a damaged page reports it; the indexer rebuilds.
        engine._report_damage(sqlite3.DatabaseError("database disk image is malformed"))
        def searchable():
            try:
                return bool(engine.search("notice period", 3))
            except NotReady:
                return False

        assert wait_for(searchable)
        assert os.path.exists(str(tmp_path / "index.db.damaged"))
    finally:
        engine.stop()
