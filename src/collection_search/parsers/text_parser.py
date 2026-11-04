from __future__ import annotations

import os
import re

from . import Section

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
MARKDOWN = {".md", ".markdown"}


def read_text(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse(path: str) -> list[Section]:
    text = read_text(path)
    markdown = os.path.splitext(path)[1].lower() in MARKDOWN
    # Location by paragraph number keeps results pointing somewhere useful.
    # In Markdown each paragraph also carries the headings above it, so a
    # result can say "Exodus 20 > 4" instead of only "paragraph 12".
    sections = []
    trail: list[tuple[int, str]] = []  # (level, title) of the headings we are under
    for i, para in enumerate(_paragraphs(text), start=1):
        if markdown:
            for line in para.split("\n"):
                m = HEADING.match(line.strip())
                if m:
                    level = len(m.group(1))
                    trail = [t for t in trail if t[0] < level] + [(level, _plain(m.group(2)))]
        sections.append(Section(para, f"paragraph {i}", " > ".join(t for _, t in trail if t)))
    return sections


def _plain(title: str) -> str:
    """Heading text without link brackets or emphasis marks."""
    title = re.sub(r"\[\[([^\]|#]*)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]", lambda m: m.group(2) or m.group(1), title)
    title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
    return title.replace("*", "").replace("_", " ").strip()


def _paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n")]
    return [p for p in paras if p]
