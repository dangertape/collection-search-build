"""App settings: collections, tool switches and preferences, in one JSON file.

Settings are the only state shared between the UI, indexer and MCP threads.
Every access goes through one lock, every change is saved at once, and
readers get copies, so a checkbox change is seen by the very next tool call.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields
from datetime import date
from typing import Callable

from .chunking import DEFAULT_OVERLAP, DEFAULT_SIZE
from .collections import Collection
from .embeddings import DEFAULT_MODEL

log = logging.getLogger(__name__)

# Change kinds sent to listeners.
INDEX = "index"  # folders, exclusions or file types changed: the indexer must sync
SCOPE = "scope"  # active switches or search-time filters changed
TOOLS = "tools"  # a tool was switched on or off

Listener = Callable[[str], None]


@dataclass
class Preferences:
    model: str = DEFAULT_MODEL
    chunk_size: int = DEFAULT_SIZE
    chunk_overlap: int = DEFAULT_OVERLAP
    default_result_count: int = 8
    max_result_count: int = 25
    rescan_minutes: int = 10
    watch_folders: bool = True


class Settings:
    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._collections: list[Collection] = []
        self._tools: dict[str, bool] = {}
        self.preferences = Preferences()
        self._index_key = _index_key(self.preferences)
        self._listeners: list[Listener] = []

    # ---- load / save ------------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "Settings":
        settings = cls(path)
        if not os.path.exists(path):
            settings.save()
            return settings
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            settings._from_dict(data)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            backup = path + ".broken"
            log.error("Settings file unreadable (%s); moved to %s and starting fresh", exc, backup)
            try:
                os.replace(path, backup)
            except OSError:
                pass
            settings = cls(path)
            settings.save()
        return settings

    def save(self) -> None:
        if not self.path:
            return
        with self._lock:
            data = self._to_dict()
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)  # atomic: a crash never leaves half a file

    def _to_dict(self) -> dict:
        return {
            "version": 1,
            "collections": [_collection_to_dict(c) for c in self._collections],
            "tools": dict(self._tools),
            "preferences": asdict(self.preferences),
        }

    def _from_dict(self, data: dict) -> None:
        self._collections = [_collection_from_dict(c) for c in data.get("collections", [])]
        self._tools = {str(k): bool(v) for k, v in data.get("tools", {}).items()}
        known = {f.name for f in fields(Preferences)}
        self.preferences = Preferences(**{k: v for k, v in data.get("preferences", {}).items() if k in known})
        self._index_key = _index_key(self.preferences)

    # ---- listeners ----------------------------------------------------------
    def subscribe(self, listener: Listener) -> None:
        self._listeners.append(listener)

    def _changed(self, *kinds: str) -> None:
        self.save()
        for kind in kinds:
            for listener in list(self._listeners):
                try:
                    listener(kind)
                except Exception:
                    log.exception("Settings listener failed")

    # ---- collections --------------------------------------------------------
    def collections(self) -> list[Collection]:
        with self._lock:
            return copy.deepcopy(self._collections)

    def active_collections(self) -> list[Collection]:
        with self._lock:
            return copy.deepcopy([c for c in self._collections if c.active])

    def get_collection(self, collection_id: str) -> Collection | None:
        with self._lock:
            found = next((c for c in self._collections if c.id == collection_id), None)
            return copy.deepcopy(found)

    def upsert_collection(self, collection: Collection) -> None:
        collection = copy.deepcopy(collection)
        collection.__post_init__()  # refresh cached path keys after edits
        with self._lock:
            for i, existing in enumerate(self._collections):
                if existing.id == collection.id:
                    needs_index = existing.index_rules() != collection.index_rules()
                    self._collections[i] = collection
                    break
            else:
                self._collections.append(collection)
                needs_index = True
        self._changed(*((INDEX, SCOPE) if needs_index else (SCOPE,)))

    def delete_collection(self, collection_id: str) -> None:
        with self._lock:
            self._collections = [c for c in self._collections if c.id != collection_id]
        self._changed(INDEX, SCOPE)

    def set_collection_active(self, collection_id: str, active: bool) -> None:
        with self._lock:
            for c in self._collections:
                if c.id == collection_id:
                    if c.active == active:
                        return
                    c.active = active
                    break
            else:
                return
        self._changed(SCOPE)

    # ---- tools ----------------------------------------------------------------
    def tool_enabled(self, name: str) -> bool:
        """Tools are on unless switched off, so a newly added tool works at once."""
        with self._lock:
            return self._tools.get(name, True)

    def enabled_tools(self, names: list[str]) -> set[str]:
        return {n for n in names if self.tool_enabled(n)}

    def set_tool_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if self._tools.get(name, True) == enabled:
                return
            self._tools[name] = enabled
        self._changed(TOOLS)

    def set_preferences(self, preferences: Preferences) -> None:
        with self._lock:
            before = self._index_key
            self.preferences = copy.deepcopy(preferences)
            self._index_key = _index_key(self.preferences)
        self._changed(INDEX if before != self._index_key else SCOPE)


def _index_key(p: Preferences) -> tuple:
    """Preferences that change what is stored in the index."""
    return (p.model, p.chunk_size, p.chunk_overlap)


def _collection_to_dict(c: Collection) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "active": c.active,
        "folders": list(c.folders),
        "excluded": list(c.excluded),
        "extensions": list(c.extensions),
        "modified_after": c.modified_after.isoformat() if c.modified_after else None,
        "modified_before": c.modified_before.isoformat() if c.modified_before else None,
        "include_patterns": list(c.include_patterns),
        "exclude_patterns": list(c.exclude_patterns),
    }


def _collection_from_dict(d: dict) -> Collection:
    def day(value):
        return date.fromisoformat(value) if value else None

    kwargs = dict(
        name=d["name"],
        folders=list(d.get("folders", [])),
        excluded=list(d.get("excluded", [])),
        modified_after=day(d.get("modified_after")),
        modified_before=day(d.get("modified_before")),
        include_patterns=list(d.get("include_patterns", [])),
        exclude_patterns=list(d.get("exclude_patterns", [])),
        active=bool(d.get("active", True)),
    )
    if d.get("extensions"):
        kwargs["extensions"] = list(d["extensions"])
    if d.get("id"):
        kwargs["id"] = d["id"]
    return Collection(**kwargs)
