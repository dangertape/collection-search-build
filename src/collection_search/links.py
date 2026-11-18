"""Connections between documents: links the user wrote in their notes.

While a file is indexed, its text is scanned for Obsidian-style [[wikilinks]],
Markdown [text](other.md) links and HTML <a href> links to other local files.
Nothing about what the documents are about is assumed: only explicit links count.

Each one is stored as a target key:
  name:<file name without extension>   for a [[wikilink]]
  path:<normalised path>               for a Markdown link to a file

A file is the target of a link when one of its own keys (file_keys) matches.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import unquote

from .parsers import Section


@dataclass(frozen=True)
class Link:
    kind: str  # always "link"
    target: str  # key, see the module docstring
    label: str  # the link text
    location: str  # where in the source file
    heading: str = ""  # headings above it in the source file


# ---- note links -------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"!?\[\[([^\]\[|#^]*)(?:[#^][^\]\[|]*)?(?:\|([^\]\[]*))?\]\]")
_MDLINK_RE = re.compile(r"(?<!!)\[([^\]\[]*)\]\(\s*<?([^)<>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def name_key(name: str) -> str:
    return "name:" + os.path.splitext(name.strip().replace("\\", "/").rsplit("/", 1)[-1])[0].strip().lower()


def extract(path: str, sections: list[Section]) -> list[Link]:
    """Every link in a parsed file, in order."""
    from .store import path_key

    links: list[Link] = []
    folder = os.path.dirname(os.path.abspath(path))
    for section in sections:
        text = section.text
        for m in _WIKILINK_RE.finditer(text):
            target = m.group(1).strip()
            if target:
                links.append(Link("link", name_key(target), (m.group(2) or target).strip(),
                                  section.location, section.heading))
        hrefs = [(m.group(2), m.group(1)) for m in _MDLINK_RE.finditer(text)] + list(section.links)
        for raw, label in hrefs:
            href = unquote(raw.split("#", 1)[0].split("?", 1)[0])
            if not href or re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.IGNORECASE):
                continue  # web and mail links, and same-page anchors
            links.append(Link("link", "path:" + path_key(os.path.join(folder, href)), label.strip() or href,
                              section.location, section.heading))
    return links


def file_keys(path: str) -> list[str]:
    """The keys under which links reach this file."""
    from .store import path_key

    return [name_key(os.path.basename(path)), "path:" + path_key(path)]
