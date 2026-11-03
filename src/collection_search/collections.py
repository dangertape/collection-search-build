"""Collections: named scopes of folders plus filters.

Folder, exclusion and file-type rules decide what is indexed. Date and
filename rules are checked again at search time, so editing them takes
effect without re-indexing.
"""

from __future__ import annotations

import fnmatch
import os
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

    def __post_init__(self) -> None:
        self._folder_keys = [path_key(f) for f in self.folders]
        self._excluded_keys = [path_key(f) for f in self.excluded]
        self._exts = {e.lower() if e.startswith(".") else "." + e.lower() for e in self.extensions}

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
        if not self.covers(key):
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


def _is_under(key: str, root: str) -> bool:
    return key == root or key.startswith(root.rstrip(os.sep) + os.sep)
