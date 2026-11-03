from __future__ import annotations

from . import Section


def parse(path: str) -> list[Section]:
    with open(path, "rb") as f:
        raw = f.read()
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Location by paragraph number keeps results pointing somewhere useful.
    sections = []
    for i, para in enumerate(_paragraphs(text), start=1):
        sections.append(Section(para, f"paragraph {i}"))
    return sections


def _paragraphs(text: str) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n")]
    return [p for p in paras if p]
