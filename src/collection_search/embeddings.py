"""Embedding models behind one small interface.

FastEmbedder is the real model (ONNX on CPU). HashEmbedder is a tiny
deterministic stand-in used by tests and for offline smoke runs.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, Protocol, Sequence

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: str | None = None, batch_size: int = 32):
        from fastembed import TextEmbedding

        self.name = model
        self._model = TextEmbedding(model_name=model, cache_dir=cache_dir)
        self._batch_size = batch_size
        self.dim = len(self.embed_query("dimension probe"))

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
