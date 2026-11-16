"""Collections: named scopes of folders plus filters.

Folder, exclusion and file-type rules decide what is indexed. Date and
filename rules, and the on/off switches on folders and documents inside a
collection, are checked at search time, so changing them takes effect
without re-indexing.
"""

from __future__ import annotations

import fnmatch
import os
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from .parsers import SUPPORTED_EXTENSIONS
from .store import path_key


@dataclass
class Collection:
    name: str
    folders: list[str]
    excluded: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=lambda: sorted(SUPPORTED_EXTENSIONS))
    modified_after: date | None = None
    modified_before: date | None = None
    include_patterns: list[str] = field(default_factory=list)  # empty = include all
    exclude_patterns: list[str] = field(default_factory=list)
    active: bool = True
    # On/off switches on folders and documents inside the collection, by path.
    # The switch nearest above a file wins; with none, a file is on.
    switches: dict[str, bool] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        self._folder_keys = [path_key(f) for f in self.folders]
        self._excluded_keys = [path_key(f) for f in self.excluded]
        self._exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in self.extensions}
        self._switch_keys = {path_key(p): bool(on) for p, on in self.switches.items()}

    def index_rules(self) -> tuple:
        """What decides which files get indexed. A change here needs a sync."""
        return (tuple(self._folder_keys), tuple(sorted(self._excluded_keys)), tuple(sorted(self._exts)))

    # Index time ---------------------------------------------------------
    def covers(self, key: str) -> bool:
        """Is this (normalised) path under a folder, outside exclusions, of an included type?"""
        if os.path.splitext(key)[1] not in self._exts:
            return False
        if not any(_is_under(key, root) for root in self._folder_keys):
            return False
        return not any(_is_under(key, ex) for ex in self._excluded_keys)

    # Search time --------------------------------------------------------
    def allows(self, key: str, mtime: float) -> bool:
        """Full check used by search: index rules plus date and filename filters."""
        if not self.covers(key) or not self.switched_on(key):
            return False
        modified = datetime.fromtimestamp(mtime).date()
        if self.modified_after and modified < self.modified_after:
            return False
        if self.modified_before and modified > self.modified_before:
            return False
        name = os.path.basename(key)
        if self.include_patterns and not any(fnmatch.fnmatch(name, p.lower()) for p in self.include_patterns):
            return False
        return not any(fnmatch.fnmatch(name, p.lower()) for p in self.exclude_patterns)

    # Folder and document switches -------------------------------------------
    def switched_on(self, key: str) -> bool:
        """Is this (normalised) path on, going by the nearest switch at or above it?"""
        return _nearest(key, self._switch_keys, True)

    def switch_state(self, key: str) -> str:
        """"on", "off" or "mixed" for a (normalised) folder or document path.

        Mixed means the item itself is on or off but something inside it is switched the other way.
        """
        own = self.switched_on(key)
        if any(on != own and k != key and _is_under(k, key) for k, on in self._switch_keys.items()):
            return "mixed"
        return "on" if own else "off"

    def has_switched_off(self) -> bool:
        """Is anything inside the collection switched off?"""
        return any(not on for on in self._switch_keys.values())

    def with_switch(self, path: str, on: bool) -> dict[str, bool]:
        """The switches after turning `path` and everything inside it on or off.

        Switches inside `path` are cleared, and a switch that only repeats what
        the folder above already says is left out, so the saved list stays short.
        """
        key = path_key(path)
        kept = {p: v for p, v in self.switches.items() if not _is_under(path_key(p), key)}
        above = _nearest(key, {path_key(p): v for p, v in kept.items()}, True)
        if above != on:
            kept[path] = on
        return kept


def _nearest(key: str, switches: dict[str, bool], default: bool) -> bool:
    """The value of the switch at `key` or the nearest folder above it."""
    if not switches:
        return default
    while True:
        if key in switches:
            return switches[key]
        parent = os.path.dirname(key)
        if parent == key:
            return default
        key = parent


def _is_under(key: str, root: str) -> bool:
    return key == root or key.startswith(root.rstrip(os.sep) + os.sep)
