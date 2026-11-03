from __future__ import annotations

import docx

from . import Section


def parse(path: str) -> list[Section]:
    """Paragraphs and table rows in document order, numbered as paragraphs."""
    document = docx.Document(path)
    body = document.element.body
    sections: list[Section] = []
    number = 0
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(node.text or "" for node in child.iter() if node.tag.endswith("}t")).strip()
            if text:
                number += 1
                sections.append(Section(text, f"paragraph {number}"))
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
                    sections.append(Section(" | ".join(cells), f"paragraph {number} (table)"))
    return sections
