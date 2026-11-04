"""Connections between documents: note links and Bible verse references.

While a file is indexed, its text is scanned for
  - Obsidian-style [[wikilinks]] and Markdown [text](other.md) links, and
  - verse references such as "Exod 20:4", "1 Cor. 8:4-6" or "John 3:16".

Each one is stored as a target key:
  name:<file name without extension>   for a [[wikilink]]
  path:<normalised path>               for a Markdown link to a file
  verse:<Book>|<chapter>               for a verse reference

A file is the target of a link when one of its own keys (file_keys) matches.
A chapter file such as "Exod-20.md" or "Exodus 20.md" has a verse key, so
every "Exodus 20:4" in any note leads to it, and it shows those notes as
backlinks.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import unquote

from .parsers import Section


@dataclass(frozen=True)
class Link:
    kind: str  # "link" or "verse"
    target: str  # key, see the module docstring
    label: str  # what the text said: link text, or a tidy reference like "Exodus 20:4-6"
    location: str  # where in the source file
    heading: str = ""  # headings above it in the source file


# ---- Bible books ---------------------------------------------------------------

# (name, abbreviations). Numbered books list the name without the number and
# the numbers they come in.
_BOOKS: list[tuple[str, tuple[int, ...], list[str]]] = [
    ("Genesis", (), ["gen", "ge", "gn"]),
    ("Exodus", (), ["exod", "exo", "ex"]),
    ("Leviticus", (), ["lev", "le", "lv"]),
    ("Numbers", (), ["num", "nu", "nm", "nb"]),
    ("Deuteronomy", (), ["deut", "deu", "de", "dt"]),
    ("Joshua", (), ["josh", "jos", "jsh"]),
    ("Judges", (), ["judg", "jdg", "jg", "jdgs"]),
    ("Ruth", (), ["rth", "ru"]),
    ("Samuel", (1, 2), ["sam", "sa", "sm"]),
    ("Kings", (1, 2), ["kgs", "ki", "kin"]),
    ("Chronicles", (1, 2), ["chron", "chr", "ch"]),
    ("Ezra", (), ["ezr"]),
    ("Nehemiah", (), ["neh", "ne"]),
    ("Esther", (), ["esth", "est", "es"]),
    ("Job", (), ["jb"]),
    ("Psalms", (), ["psalm", "psa", "pss", "psm", "ps"]),
    ("Proverbs", (), ["prov", "pro", "prv", "pr"]),
    ("Ecclesiastes", (), ["eccles", "eccl", "ecc", "ec", "qoh"]),
    ("Song of Solomon", (), ["song of songs", "song", "sos", "sng", "canticles"]),
    ("Isaiah", (), ["isa"]),
    ("Jeremiah", (), ["jer", "je", "jr"]),
    ("Lamentations", (), ["lam", "la"]),
    ("Ezekiel", (), ["ezek", "eze", "ezk"]),
    ("Daniel", (), ["dan", "da", "dn"]),
    ("Hosea", (), ["hos", "ho"]),
    ("Joel", (), ["jl"]),
    ("Amos", (), []),
    ("Obadiah", (), ["obad", "ob"]),
    ("Jonah", (), ["jon", "jnh"]),
    ("Micah", (), ["mic", "mc"]),
    ("Nahum", (), ["nah", "na"]),
    ("Habakkuk", (), ["hab", "hb"]),
    ("Zephaniah", (), ["zeph", "zep", "zp"]),
    ("Haggai", (), ["hag", "hg"]),
    ("Zechariah", (), ["zech", "zec", "zc"]),
    ("Malachi", (), ["mal", "ml"]),
    ("Matthew", (), ["matt", "mat", "mt"]),
    ("Mark", (), ["mrk", "mk", "mr"]),
    ("Luke", (), ["luk", "lk"]),
    ("John", (), ["jhn", "joh", "jn"]),
    ("Acts", (), ["act", "ac"]),
    ("Romans", (), ["rom", "ro", "rm"]),
    ("Corinthians", (1, 2), ["cor", "co"]),
    ("Galatians", (), ["gal", "ga"]),
    ("Ephesians", (), ["ephes", "eph"]),
    ("Philippians", (), ["phil", "php", "pp"]),
    ("Colossians", (), ["col"]),
    ("Thessalonians", (1, 2), ["thess", "thes", "th"]),
    ("Timothy", (1, 2), ["tim", "ti"]),
    ("Titus", (), ["tit"]),
    ("Philemon", (), ["philem", "phlm", "phm"]),
    ("Hebrews", (), ["heb"]),
    ("James", (), ["jas", "jm"]),
    ("Peter", (1, 2), ["pet", "pe", "pt"]),
    ("John", (1, 2, 3), ["jhn", "joh", "jn"]),  # the letters; plain "John" is the gospel
    ("Jude", (), ["jud"]),
    ("Revelation", (), ["revelations", "rev", "re", "apocalypse"]),
]

_NUMBER_WORDS = {"i": "1", "ii": "2", "iii": "3", "first": "1", "second": "2", "third": "3",
                 "1st": "1", "2nd": "2", "3rd": "3"}


def _squash(text: str) -> str:
    return re.sub(r"[\s._\-]+", "", text.lower())


def _build_book_index() -> tuple[dict[str, str], list[str]]:
    index: dict[str, str] = {}  # squashed spelling -> canonical name
    words: set[str] = set()  # spellings without the number, for the regex
    for name, numbers, abbrevs in _BOOKS:
        spellings = [name.lower(), *abbrevs]
        words.update(spellings)
        for spelling in spellings:
            if numbers:
                for n in numbers:
                    index.setdefault(f"{n}{_squash(spelling)}", f"{n} {name}")
            else:
                index.setdefault(_squash(spelling), name)
    return index, sorted(words, key=len, reverse=True)


_BOOK_INDEX, _BOOK_WORDS = _build_book_index()
# A digit may touch the name ("1Cor"); a roman numeral or word must not ("Isa" is not "I Sa").
_BOOK = r"(?:(?P<num>[123])[\s.]*|(?P<word>iii|ii|i|first|second|third|1st|2nd|3rd)\s+)?(?P<book>" + "|".join(
    re.escape(w).replace(r"\ ", r"\s+") for w in _BOOK_WORDS) + r")"
_VERSE_RE = re.compile(
    r"(?<![\w])" + _BOOK + r"\.?\s*(?P<ch>\d{1,3})\s*:\s*(?P<v1>\d{1,3})(?:\s*[-–—]\s*(?P<v2>\d{1,3})(?!\s*:))?",
    re.IGNORECASE,
)
_CHAPTER_FILE_RE = re.compile(r"(?:\d{1,3}\s*[-_.]\s*)?" + _BOOK + r"\.?[\s\-_.]*(?P<ch>\d{1,3})", re.IGNORECASE)


def book_name(num: str | None, book: str) -> str | None:
    """Canonical book name ("1 Corinthians") for a spelling, or None."""
    prefix = _NUMBER_WORDS.get((num or "").lower(), num or "")
    return _BOOK_INDEX.get(prefix + _squash(book))


def verse_references(text: str) -> list[tuple[str, int, str]]:
    """(book, chapter, tidy label) for each chapter:verse reference in the text."""
    found = []
    for m in _VERSE_RE.finditer(text):
        book_text = m.group("book")
        if not (book_text[0].isupper() or m.group("num") or m.group("word")):
            continue  # "is 3:30" is a time, "Isa 3:30" is a verse
        name = book_name(m.group("num") or m.group("word"), book_text)
        if not name:
            continue
        chapter, v1, v2 = int(m.group("ch")), m.group("v1"), m.group("v2")
        label = f"{name} {chapter}:{v1}" + (f"-{v2}" if v2 else "")
        found.append((name, chapter, label))
    return found


def chapter_of(path: str) -> tuple[str, int] | None:
    """(book, chapter) when the file name names one Bible chapter, like "Exod-20.md"."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = _CHAPTER_FILE_RE.fullmatch(stem.strip())
    if not m:
        return None
    name = book_name(m.group("num") or m.group("word"), m.group("book"))
    return (name, int(m.group("ch"))) if name else None


def verse_key(book: str, chapter: int) -> str:
    return f"verse:{book}|{chapter}"


# ---- note links -------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"!?\[\[([^\]\[|#^]*)(?:[#^][^\]\[|]*)?(?:\|([^\]\[]*))?\]\]")
_MDLINK_RE = re.compile(r"(?<!!)\[([^\]\[]*)\]\(\s*<?([^)<>\s]+)>?(?:\s+\"[^\"]*\")?\s*\)")


def name_key(name: str) -> str:
    return "name:" + os.path.splitext(name.strip().replace("\\", "/").rsplit("/", 1)[-1])[0].strip().lower()


def extract(path: str, sections: list[Section]) -> list[Link]:
    """Every link and verse reference in a parsed file, in order."""
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
        for m in _MDLINK_RE.finditer(text):
            href = unquote(m.group(2).split("#", 1)[0])
            if not href or re.match(r"^[a-z][a-z0-9+.\-]*:", href, re.IGNORECASE):
                continue  # web and mail links, and same-page anchors
            label = m.group(1).strip() or href
            links.append(Link("link", "path:" + path_key(os.path.join(folder, href)), label,
                              section.location, section.heading))
        for book, chapter, label in verse_references(text):
            links.append(Link("verse", verse_key(book, chapter), label, section.location, section.heading))
    return links


def file_keys(path: str) -> list[str]:
    """The keys under which links reach this file."""
    from .store import path_key

    keys = [name_key(os.path.basename(path)), "path:" + path_key(path)]
    chapter = chapter_of(path)
    if chapter:
        keys.append(verse_key(*chapter))
    return keys
