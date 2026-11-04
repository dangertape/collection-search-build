"""Turn a file into text sections, each tagged with where it came from.

Each parser lives in its own module and registers the extensions it handles.
Adding a document type means adding one module and one line to PARSERS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Section:
    """A piece of a document: its text and a human-readable location."""

    text: str
    location: str  # e.g. "page 3" or "paragraph 12"
    heading: str = ""  # the headings above this text, e.g. "Exodus 20 > 4" (Markdown and Word)


class ParseError(Exception):
    """A file could not be read as text."""


def _registry() -> dict[str, Callable[[str], list[Section]]]:
    from . import docx_parser, pdf_parser, text_parser

    return {
        ".pdf": pdf_parser.parse,
        ".docx": docx_parser.parse,
        ".txt": text_parser.parse,
        ".md": text_parser.parse,
        ".markdown": text_parser.parse,
    }


PARSERS = _registry()
SUPPORTED_EXTENSIONS = frozenset(PARSERS)


def parse(path: str) -> list[Section]:
    """Parse a file into sections. Raises ParseError if it cannot be read."""
    ext = os.path.splitext(path)[1].lower()
    parser = PARSERS.get(ext)
    if parser is None:
        raise ParseError(f"Unsupported file type: {ext or '(none)'}")
    try:
        return parser(path)
    except ParseError:
        raise
    except Exception as exc:  # parsers wrap many third-party errors
        raise ParseError(f"{type(exc).__name__}: {exc}") from exc
