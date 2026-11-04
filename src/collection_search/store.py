"""SQLite index: files, chunks, vectors (sqlite-vec) and a keyword index (FTS5).

Vectors live in a plain table and are ranked with sqlite-vec's distance
function, so a search can be restricted to an exact set of files in SQL
before anything is ranked. Personal collections are small enough that an
exact scan is fast; an approximate index can come later if needed.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import sqlite_vec

from .chunking import Chunk

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class FileRecord:
    id: int
    path: str
    size: int
    mtime: float


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: int
    path: str
    location: str
    text: str
    score: float


def default_db_path() -> str:
    from .paths import index_path

    return index_path()


class Store:
    """One connection per thread: open a Store in each thread that needs it."""

    def __init__(self, path: str, model_name: str, dim: int, reset_if_model_changed: bool = False):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        try:
            self._open(model_name, dim, reset_if_model_changed)
        except BaseException:
            self.db.close()  # don't leave the file held open (it may need moving aside)
            raise

    def _open(self, model_name: str, dim: int, reset_if_model_changed: bool) -> None:
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        try:
            self._check_model(model_name, dim)
        except ModelMismatch:
            if not reset_if_model_changed:
                raise
            self.reset(model_name, dim)
        self.dim = dim
        self._backfill_doc_vectors()

    def close(self) -> None:
        self.db.close()

    # ---- schema -------------------------------------------------------
    def _create_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                path_key TEXT NOT NULL UNIQUE,   -- normalised for comparison (case, separators)
                size INTEGER NOT NULL,
                mtime REAL NOT NULL,
                indexed_at REAL NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                ord INTEGER NOT NULL,
                location TEXT NOT NULL,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
            CREATE TABLE IF NOT EXISTS vectors (
                chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                embedding BLOB NOT NULL
            );
            -- One vector per file: the normalised mean of its chunk vectors.
            -- Used to find documents like a given one (find_similar).
            CREATE TABLE IF NOT EXISTS doc_vectors (
                file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                embedding BLOB NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            """
        )
        self.db.commit()

    def _check_model(self, model_name: str, dim: int) -> None:
        row = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        if not row:
            self.db.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [("schema", str(SCHEMA_VERSION)), ("model", model_name), ("dim", str(dim))],
            )
            self.db.commit()
        elif row.get("model") != model_name or row.get("dim") != str(dim):
            raise ModelMismatch(
                f"Index was built with {row.get('model')} ({row.get('dim')} dims); "
                f"current model is {model_name} ({dim} dims). Rebuild the index."
            )

    def _backfill_doc_vectors(self) -> None:
        """Indexes built before document vectors existed get them from their chunk vectors."""
        missing = [r[0] for r in self.db.execute(
            "SELECT DISTINCT c.file_id FROM chunks c LEFT JOIN doc_vectors d ON d.file_id = c.file_id "
            "WHERE d.file_id IS NULL"
        )]
        if not missing:
            return
        with self.db:
            for file_id in missing:
                _, _, _, matrix = self.chunk_vectors(file_id)
                vec = document_vector(matrix)
                if vec is not None:
                    self.db.execute("INSERT OR REPLACE INTO doc_vectors(file_id, embedding) VALUES (?, ?)",
                                    (file_id, vec.tobytes()))

    def reset(self, model_name: str, dim: int) -> None:
        """Empty the index (keeps the file, which may be open in other threads)."""
        with self.db:
            self.db.execute("DELETE FROM files")
            self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
            self.db.execute("DELETE FROM meta")
            self.db.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [("schema", str(SCHEMA_VERSION)), ("model", model_name), ("dim", str(dim))],
            )
        self.dim = dim

    # ---- files ----------------------------------------------------------
    def files(self) -> dict[str, FileRecord]:
        rows = self.db.execute("SELECT id, path, path_key, size, mtime FROM files").fetchall()
        return {key: FileRecord(id, path, size, mtime) for id, path, key, size, mtime in rows}

    def file(self, key: str) -> FileRecord | None:
        row = self.db.execute("SELECT id, path, size, mtime FROM files WHERE path_key = ?", (key,)).fetchone()
        return FileRecord(*row) if row else None

    def replace_file(
        self,
        path: str,
        size: int,
        mtime: float,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        error: str | None = None,
    ) -> None:
        """Insert or replace one file and all its chunks in a single transaction."""
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        key = path_key(path)
        doc_vec = document_vector(np.asarray(vectors, dtype=np.float32)) if len(vectors) else None
        with self.db:
            self.db.execute("DELETE FROM files WHERE path_key = ?", (key,))
            cur = self.db.execute(
                "INSERT INTO files(path, path_key, size, mtime, indexed_at, error) VALUES (?, ?, ?, ?, ?, ?)",
                (path, key, size, mtime, time.time(), error),
            )
            file_id = cur.lastrowid
            for ord_, (chunk, vec) in enumerate(zip(chunks, vectors)):
                cid = self.db.execute(
                    "INSERT INTO chunks(file_id, ord, location, text) VALUES (?, ?, ?, ?)",
                    (file_id, ord_, chunk.location, chunk.text),
                ).lastrowid
                self.db.execute(
                    "INSERT INTO vectors(chunk_id, embedding) VALUES (?, ?)",
                    (cid, sqlite_vec.serialize_float32(list(vec))),
                )
            if doc_vec is not None:
                self.db.execute("INSERT INTO doc_vectors(file_id, embedding) VALUES (?, ?)",
                                (file_id, doc_vec.tobytes()))

    def remove_files(self, keys: Iterable[str]) -> None:
        with self.db:
            self.db.executemany("DELETE FROM files WHERE path_key = ?", [(k,) for k in keys])

    def stats(self) -> dict[str, int]:
        files = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        errors = self.db.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"files": files, "files_with_errors": errors, "chunks": chunks}

    def errors(self) -> list[tuple[str, str]]:
        return self.db.execute("SELECT path, error FROM files WHERE error IS NOT NULL ORDER BY path").fetchall()

    # ---- search -------------------------------------------------------
    # Both queries take the allowed file ids and filter on them in SQL before
    # ranking, so chunks from other files are never scored or returned.
    def _with_allowed(self, file_ids: Iterable[int]) -> None:
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS allowed(id INTEGER PRIMARY KEY)")
        self.db.execute("DELETE FROM temp.allowed")
        self.db.executemany("INSERT OR IGNORE INTO temp.allowed(id) VALUES (?)", [(i,) for i in file_ids])

    def vector_search(self, query_vec: Sequence[float], file_ids: Iterable[int], limit: int) -> list[ChunkHit]:
        self._with_allowed(file_ids)
        rows = self.db.execute(
            """
            SELECT c.id, f.path, c.location, c.text,
                   vec_distance_cosine(v.embedding, ?) AS distance
            FROM chunks c
            JOIN temp.allowed a ON a.id = c.file_id
            JOIN files f ON f.id = c.file_id
            JOIN vectors v ON v.chunk_id = c.id
            ORDER BY distance
            LIMIT ?
            """,
            (sqlite_vec.serialize_float32(list(query_vec)), limit),
        ).fetchall()
        # A zero query vector has no direction; its distances come back NULL.
        return [ChunkHit(cid, path, loc, text, 1.0 - dist) for cid, path, loc, text, dist in rows if dist is not None]

    def keyword_search(self, query: str, file_ids: Iterable[int], limit: int) -> list[ChunkHit]:
        match = fts_query(query)
        if not match:
            return []
        self._with_allowed(file_ids)
        rows = self.db.execute(
            """
            SELECT c.id, f.path, c.location, c.text, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN temp.allowed a ON a.id = c.file_id
            JOIN files f ON f.id = c.file_id
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        return [ChunkHit(cid, path, loc, text, -rank) for cid, path, loc, text, rank in rows]

    # ---- similar documents ------------------------------------------------
    def doc_vector(self, file_id: int) -> np.ndarray | None:
        row = self.db.execute("SELECT embedding FROM doc_vectors WHERE file_id = ?", (file_id,)).fetchone()
        return np.frombuffer(row[0], dtype=np.float32) if row else None

    def similar_files(self, vec: Sequence[float], file_ids: Iterable[int], limit: int) -> list[tuple[int, float]]:
        """(file id, cosine similarity) of the allowed files whose document vector is nearest."""
        self._with_allowed(file_ids)
        rows = self.db.execute(
            """
            SELECT d.file_id, vec_distance_cosine(d.embedding, ?) AS distance
            FROM doc_vectors d
            JOIN temp.allowed a ON a.id = d.file_id
            ORDER BY distance
            LIMIT ?
            """,
            (sqlite_vec.serialize_float32([float(x) for x in vec]), limit),
        ).fetchall()
        return [(fid, 1.0 - dist) for fid, dist in rows if dist is not None]

    def chunk_vectors(self, file_id: int) -> tuple[list[int], list[str], list[str], np.ndarray]:
        """Chunk ids, locations, texts and an (n, dim) matrix of one file's chunks, in order."""
        rows = self.db.execute(
            "SELECT c.id, c.location, c.text, v.embedding FROM chunks c JOIN vectors v ON v.chunk_id = c.id "
            "WHERE c.file_id = ? ORDER BY c.ord",
            (file_id,),
        ).fetchall()
        dim = getattr(self, "dim", 0)
        if not rows:
            return [], [], [], np.zeros((0, dim), dtype=np.float32)
        matrix = np.stack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
        return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], matrix


def document_vector(chunk_vectors: np.ndarray) -> np.ndarray | None:
    """Mean of the unit-length chunk vectors, scaled to unit length. None if there is nothing to average."""
    if chunk_vectors.size == 0:
        return None
    norms = np.linalg.norm(chunk_vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mean = (chunk_vectors / norms).mean(axis=0)
    length = float(np.linalg.norm(mean))
    if length == 0:
        return None
    return (mean / length).astype(np.float32)


class ModelMismatch(Exception):
    pass


def path_key(path: str) -> str:
    """Normalise a path for comparison: absolute, real, case-folded on Windows."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def fts_query(query: str) -> str:
    """Quote each word so user text can't use FTS5 syntax; match any word."""
    import re

    words = re.findall(r"\w+", query)
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
