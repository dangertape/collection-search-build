"""Phase 1 test harness: index a folder, search it, and score test questions.

Examples:
    python -m collection_search.cli index "D:\\Clients"
    python -m collection_search.cli search "notice period in the lease"
    python -m collection_search.cli eval questions.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

from .chunking import DEFAULT_OVERLAP, DEFAULT_SIZE
from .collections import Collection
from .embeddings import DEFAULT_MODEL, FastEmbedder, HashEmbedder
from .indexer import sync
from .search import search
from .store import Store, default_db_path

FOLDERS_FILE = "folders.txt"  # remembered next to the index so search/eval know the scope


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="collection-search-cli", description=__doc__.splitlines()[0])
    p.add_argument("--db", default=default_db_path(), help="index file (default: %(default)s)")
    p.add_argument("--model", default=DEFAULT_MODEL, help="fastembed model name")
    p.add_argument("--offline-test-embedder", action="store_true", help="word-hash embedder, for smoke tests only")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("index", help="index one or more folders")
    pi.add_argument("folders", nargs="+")
    pi.add_argument("--chunk-size", type=int, default=DEFAULT_SIZE)
    pi.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)

    ps = sub.add_parser("search", help="search the index")
    ps.add_argument("query")
    ps.add_argument("-k", "--limit", type=int, default=5)
    ps.add_argument("--no-keywords", action="store_true", help="semantic only")

    pe = sub.add_parser("eval", help="score a CSV of question,expected_file")
    pe.add_argument("csv")
    pe.add_argument("-k", "--limit", type=int, default=5)
    pe.add_argument("--no-keywords", action="store_true")

    sub.add_parser("status", help="show index counts and parse errors")

    args = p.parse_args(argv)
    embedder = HashEmbedder() if args.offline_test_embedder else FastEmbedder(args.model)
    store = Store(args.db, embedder.name, embedder.dim)
    try:
        return COMMANDS[args.cmd](args, store, embedder)
    finally:
        store.close()


def _folders_path(db: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db)), FOLDERS_FILE)


def _collection(args) -> Collection:
    path = _folders_path(args.db)
    if not os.path.exists(path):
        sys.exit("No folders indexed yet. Run the index command first.")
    with open(path, encoding="utf-8") as f:
        folders = [line.strip() for line in f if line.strip()]
    return Collection("cli", folders)


def cmd_index(args, store, embedder) -> int:
    folders = [os.path.abspath(f) for f in args.folders]
    for f in folders:
        if not os.path.isdir(f):
            sys.exit(f"Not a folder: {f}")
    with open(_folders_path(args.db), "w", encoding="utf-8") as fh:
        fh.write("\n".join(folders) + "\n")

    started = time.time()

    def progress(done, total, path):
        if total:
            print(f"\r[{done}/{total}] {os.path.basename(path)[:60]:<60}", end="", flush=True)

    result = sync(store, embedder, [Collection("cli", folders)], progress, args.chunk_size, args.overlap)
    elapsed = time.time() - started
    print(
        f"\nAdded {result.added}, updated {result.updated}, removed {result.removed}, "
        f"unchanged {result.unchanged} in {elapsed:.1f}s."
    )
    for path, err in result.errors:
        print(f"  ! {path}: {err}")
    stats = store.stats()
    print(f"Index: {stats['files']} files, {stats['chunks']} chunks.")
    return 0


def cmd_search(args, store, embedder) -> int:
    results = search(store, embedder, args.query, [_collection(args)], args.limit, not args.no_keywords)
    if not results:
        print("No results.")
    for i, r in enumerate(results, 1):
        snippet = " ".join(r.text.split())[:300]
        print(f"{i}. {r.path}  ({r.location}, score {r.score})\n   {snippet}\n")
    return 0


def cmd_eval(args, store, embedder) -> int:
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].startswith("#")]
    if rows and rows[0][0].strip().lower() == "question":
        rows = rows[1:]
    collection = _collection(args)
    hits, rr, misses = 0, 0.0, []
    for row in rows:
        question, expected = row[0].strip(), row[1].strip().lower()
        results = search(store, embedder, question, [collection], args.limit, not args.no_keywords)
        rank = next((i for i, r in enumerate(results, 1) if expected in os.path.basename(r.path).lower()), None)
        if rank:
            hits += 1
            rr += 1.0 / rank
        else:
            misses.append((question, expected, [os.path.basename(r.path) for r in results[:3]]))
    n = len(rows) or 1
    print(f"Top-{args.limit} hit rate: {hits}/{len(rows)} = {hits / n:.0%}   MRR: {rr / n:.3f}")
    for q, exp, got in misses:
        print(f"  miss: {q!r} expected {exp!r}, got {got}")
    return 0


def cmd_status(args, store, embedder) -> int:
    stats = store.stats()
    print(f"{stats['files']} files, {stats['chunks']} chunks, {stats['files_with_errors']} with errors.")
    for path, err in store.errors():
        print(f"  ! {path}: {err}")
    return 0


COMMANDS = {"index": cmd_index, "search": cmd_search, "eval": cmd_eval, "status": cmd_status}

if __name__ == "__main__":
    raise SystemExit(main())
