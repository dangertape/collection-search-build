from __future__ import annotations

import docx
from docx.text.paragraph import Paragraph

from . import Section


def parse(path: str) -> list[Section]:
    """Paragraphs and table rows in document order, numbered as paragraphs."""
    document = docx.Document(path)
    body = document.element.body
    sections: list[Section] = []
    number = 0
    trail: list[tuple[int, str]] = []  # headings we are under, from "Heading N" styles
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(node.text or "" for node in child.iter() if node.tag.endswith("}t")).strip()
            if text:
                level = _heading_level(child, document)
                if level:
                    trail = [t for t in trail if t[0] < level] + [(level, text)]
                number += 1
                sections.append(Section(text, f"paragraph {number}", " > ".join(t for _, t in trail)))
        elif tag == "tbl":
            table = docx.table.Table(child, document)
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    value = cell.text.strip()
                    if value and (not cells or cells[-1] != value):  # merged cells repeat
                        cells.append(value)
                if cells:
                    number += 1
                    sections.append(Section(" | ".join(cells), f"paragraph {number} (table)",
                                            " > ".join(t for _, t in trail)))
    return sections


def _heading_level(p, document) -> int:
    """1-9 for a paragraph styled "Heading N" (or "Title" as 1), else 0."""
    try:
        name = Paragraph(p, document).style.name or ""
    except Exception:
        return 0
    if name == "Title":
        return 1
    parts = name.split()
    if len(parts) == 2 and parts[0] == "Heading" and parts[1].isdigit():
        return int(parts[1])
    return 0
