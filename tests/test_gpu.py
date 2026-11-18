"""Graphics-card embedding, batching across files, and the model upgrade."""

import json
import sys
import types

from collection_search import embeddings
from collection_search.collections import Collection
from collection_search.embeddings import DEFAULT_MODEL, OLD_DEFAULT_MODEL, HashEmbedder
from collection_search.indexer import sync
from collection_search.settings import Settings

from conftest import write


class Counting(HashEmbedder):
    def __init__(self, preferred_batch):
        super().__init__()
        self.preferred_batch = preferred_batch
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(len(texts))
        return super().embed_documents(texts)


def _folder(tmp_path, n):
    for i in range(n):
        write(str(tmp_path / "docs" / f"note{i:02}.md"), f"# Note {i}\n\nPassage about {['river', 'mountain', 'harvest', 'temple', 'garden', 'desert'][i % 6]} number{i} and code{i * 37}.")
    return [Collection(name="Docs", folders=[str(tmp_path / "docs")])]


def test_passages_from_many_files_share_one_model_call(tmp_path, store):
    collections = _folder(tmp_path, 20)
    embedder = Counting(preferred_batch=8)
    result = sync(store, embedder, collections)
    assert result.added == 20 and not result.errors
    assert len(embedder.calls) < 20 and max(embedder.calls) >= 8
    assert store.stats()["files"] == 20 and store.stats()["chunks"] == 20


def test_batched_vectors_land_on_the_right_files(tmp_path, store):
    collections = _folder(tmp_path, 12)
    sync(store, Counting(preferred_batch=5), collections)
    ids = [r[0] for r in store.db.execute("SELECT id FROM files")]
    for cid, text, path in store.db.execute(
            "SELECT c.id, c.text, f.path FROM chunks c JOIN files f ON f.id = c.file_id").fetchall():
        best = store.vector_search(HashEmbedder().embed_query(text), ids, 1)[0]
        assert best.chunk_id == cid and best.path == path and best.score > 0.999


def test_cancel_stops_between_files(tmp_path, store):
    collections = _folder(tmp_path, 10)
    seen = []
    result = sync(store, Counting(preferred_batch=1), collections,
                  progress=lambda d, t, p: seen.append(d), cancelled=lambda: len(seen) >= 3)
    assert result.cancelled
    assert store.stats()["files"] < 10
    assert sync(store, HashEmbedder(), collections).added + store.stats()["files"] >= 10


def test_unreadable_files_still_reported(tmp_path, store):
    collections = _folder(tmp_path, 3)
    write(str(tmp_path / "docs" / "empty.md"), "   ")
    result = sync(store, Counting(preferred_batch=100), collections)
    assert [p for p, _ in result.errors] == [str(tmp_path / "docs" / "empty.md")]
    assert result.added == 4


def test_old_default_model_moves_to_new_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 2, "preferences": {"model": OLD_DEFAULT_MODEL}}))
    assert Settings.load(str(path)).preferences.model == DEFAULT_MODEL


def test_chosen_model_is_kept(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 2, "preferences": {"model": "intfloat/multilingual-e5-large"}}))
    settings = Settings.load(str(path))
    assert settings.preferences.model == "intfloat/multilingual-e5-large"
    assert settings.preferences.use_gpu is True


class _FakeModel:
    fail_on = ()

    def __init__(self, model_name, cache_dir=None, providers=None):
        if providers[0] in self.fail_on:
            raise RuntimeError("DirectML can't run this")
        self.providers = providers

    def query_embed(self, text):
        import numpy as np

        yield np.zeros(4)

    def passage_embed(self, texts, batch_size):
        import numpy as np

        for _ in texts:
            yield np.zeros(4)


def _fake_fastembed(monkeypatch, gpu, fail_on=()):
    module = types.ModuleType("fastembed")
    module.TextEmbedding = type("TextEmbedding", (_FakeModel,), {"fail_on": fail_on})
    monkeypatch.setitem(sys.modules, "fastembed", module)
    monkeypatch.setattr(embeddings, "gpu_available", lambda: gpu)


def test_runs_on_graphics_card_when_available(monkeypatch):
    _fake_fastembed(monkeypatch, gpu=True)
    e = embeddings.FastEmbedder("m")
    assert e.device == "GPU (DirectML)" and e._model.providers == [embeddings.GPU_PROVIDER]
    assert e.preferred_batch > embeddings.CPU_BATCH


def test_falls_back_to_cpu_when_card_fails(monkeypatch):
    _fake_fastembed(monkeypatch, gpu=True, fail_on=(embeddings.GPU_PROVIDER,))
    e = embeddings.FastEmbedder("m")
    assert e.device == "CPU" and e._model.providers == [embeddings.CPU_PROVIDER] and e.dim == 4


def test_cpu_when_no_card_or_switched_off(monkeypatch):
    _fake_fastembed(monkeypatch, gpu=False)
    assert embeddings.FastEmbedder("m").device == "CPU"
    _fake_fastembed(monkeypatch, gpu=True)
    assert embeddings.FastEmbedder("m", use_gpu=False).device == "CPU"
