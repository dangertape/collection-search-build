"""Split parsed sections into overlapping chunks sized for embedding.

Chunks are built from paragraphs and may span several sections (short Word
paragraphs, for example). Each chunk records the range of sections it covers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .parsers import Section

DEFAULT_SIZE = 800  # characters
DEFAULT_OVERLAP = 150


@dataclass(frozen=True)
class Chunk:
    text: str
    location: str
    heading: str = ""  # headings above the chunk's text, e.g. "Exodus 20 > 1-4"


@dataclass
class _Piece:
    text: str
    location: str
    heading: str = ""


def chunk_sections(
    sections: list[Section], size: int = DEFAULT_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[Chunk]:
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    pieces = list(_pieces(sections, size))
    chunks: list[Chunk] = []
    current: list[_Piece] = []
    length = 0
    for piece in pieces:
        added = len(piece.text) + (2 if current else 0)
        if current and length + added > size:
            chunks.append(_make_chunk(current))
            current = _overlap_tail(current, overlap)
            length = sum(len(p.text) for p in current) + 2 * max(len(current) - 1, 0)
            added = len(piece.text) + (2 if current else 0)
        current.append(piece)
        length += added
    if current and not _is_only_overlap(current, chunks):
        chunks.append(_make_chunk(current))
    return chunks


def _pieces(sections: list[Section], size: int):
    """Paragraphs of each section; any paragraph longer than size is split by sentence, then by words."""
    for section in sections:
        for para in re.split(r"\n\s*\n", section.text):
            para = " ".join(para.split())
            if not para:
                continue
            if len(para) <= size:
                yield _Piece(para, section.location, section.heading)
            else:
                for part in _split_long(para, size):
                    yield _Piece(part, section.location, section.heading)


def _split_long(text: str, size: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    parts: list[str] = []
    buf = ""
    for sentence in sentences:
        while len(sentence) > size:  # a single huge "sentence": cut at a space
            cut = sentence.rfind(" ", 0, size)
            cut = cut if cut > 0 else size
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        candidate = f"{buf} {sentence}".strip()
        if len(candidate) > size and buf:
            parts.append(buf)
            buf = sentence
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts


def _overlap_tail(pieces: list[_Piece], overlap: int) -> list[_Piece]:
    """The end of the previous chunk, about `overlap` characters, cut at a word boundary."""
    if overlap <= 0:
        return []
    last = pieces[-1]
    if len(last.text) <= overlap:
        return [_Piece(last.text, last.location, last.heading)]
    tail = last.text[-overlap:]
    space = tail.find(" ")
    if 0 <= space < len(tail) - 1:
        tail = tail[space + 1 :]
    return [_Piece(tail, last.location, last.heading)]


def _is_only_overlap(current: list[_Piece], chunks: list[Chunk]) -> bool:
    return bool(chunks) and len(current) == 1 and chunks[-1].text.endswith(current[0].text)


def _make_chunk(pieces: list[_Piece]) -> Chunk:
    text = "\n\n".join(p.text for p in pieces)
    return Chunk(text, _location_range(pieces[0].location, pieces[-1].location),
                 heading_range(pieces[0].heading, pieces[-1].heading))


def heading_range(first: str, last: str) -> str:
    """"A > B > 1" and "A > B > 4" become "A > B > 1-4"; the shared part is said once."""
    if first == last or not last:
        return first
    if not first:
        return last
    a, b = first.split(" > "), last.split(" > ")
    same = 0
    while same < min(len(a), len(b)) - 1 and a[same] == b[same]:
        same += 1
    head = " > ".join(a[:same])
    tail = f"{' > '.join(a[same:])}-{' > '.join(b[same:])}"
    return f"{head} > {tail}" if head else tail


def _location_range(first: str, last: str) -> str:
    if first == last:
        return first
    m1 = re.fullmatch(r"(page|paragraph) (\d+)(.*)", first)
    m2 = re.fullmatch(r"(page|paragraph) (\d+)(.*)", last)
    if m1 and m2 and m1.group(1) == m2.group(1):
        return f"{m1.group(1)}s {m1.group(2)}-{m2.group(2)}"
    return f"{first} to {last}"
