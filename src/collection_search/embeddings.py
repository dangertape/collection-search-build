"""Embedding models behind one small interface.

FastEmbedder is the real model (ONNX). On Windows builds it runs on the
graphics card through DirectML when one is available and falls back to the
CPU otherwise. HashEmbedder is a tiny deterministic stand-in used by tests
and for offline smoke runs.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Iterable, Protocol, Sequence

DEFAULT_MODEL = "BAAI/bge-base-en-v1.5"
# The default before version 0.3. Settings still naming it move to DEFAULT_MODEL.
OLD_DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

GPU_PROVIDER = "DmlExecutionProvider"  # DirectML, from the onnxruntime-directml package
CPU_PROVIDER = "CPUExecutionProvider"
GPU_BATCH = 64  # passages per model call on the graphics card
CPU_BATCH = 16

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    dim: int
    # Optional extras: `device` ("GPU (DirectML)" or "CPU") for the Status tab, and
    # `preferred_batch`, how many passages the indexer should hand over per call.

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def gpu_available() -> bool:
    try:
        import onnxruntime as ort

        return GPU_PROVIDER in ort.get_available_providers()
    except Exception:  # a broken onnxruntime install must not stop the app
        return False


class FastEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str | None = None, use_gpu: bool = True):
        self.name = model
        self._model = None
        if use_gpu and gpu_available():
            try:
                self._load(model, cache_dir, [GPU_PROVIDER], GPU_BATCH, GPU_BATCH * 4)
                self.device = "GPU (DirectML)"
            except Exception as exc:  # old driver, unsupported card, an operator DirectML can't run...
                log.warning("Could not run the search model on the graphics card, using the CPU: %s", exc)
                self._model = None
        if self._model is None:
            # One file at a time on the CPU: a big shared batch gains nothing there and
            # would make searches wait longer behind the indexer.
            self._load(model, cache_dir, [CPU_PROVIDER], CPU_BATCH, CPU_BATCH)
            self.device = "CPU"
        log.info("Search model %s running on %s", model, self.device)

    def _load(self, model: str, cache_dir: str | None, providers: list[str], batch: int, shared: int) -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model, cache_dir=cache_dir, providers=providers)
        self._batch_size = batch
        self.preferred_batch = shared
        # A probe at a realistic batch size, so a provider that loads but can't run fails here.
        self.dim = len(self.embed_query("dimension probe"))
        self.embed_documents(["probe passage " * 40] * 2)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(list(texts), batch_size=self._batch_size)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()


class HashEmbedder:
    """Bag-of-words hashed into a fixed vector. Matches shared words, nothing more."""

    def __init__(self, dim: int = 256):
        self.name = f"hash-{dim}"
        self.dim = dim

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for word in _words(text):
            h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "little")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]


def _words(text: str) -> Iterable[str]:
    return re.findall(r"\w+", text.lower())
