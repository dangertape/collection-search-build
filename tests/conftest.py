import os

import docx
import pytest

from collection_search.embeddings import HashEmbedder
from collection_search.store import Store


def make_pdf(path: str, pages: list[str]) -> None:
    """Write a minimal valid PDF with one line of text per page (no extra deps)."""
    objects = []
    kids = []
    n_pages = len(pages)
    # 1: catalog, 2: pages, 3: font, then page/content pairs
    for i, text in enumerate(pages):
        page_id = 4 + 2 * i
        kids.append(f"{page_id} 0 R")
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n_pages} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content_id = 5 + 2 * i
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET" if text else ""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{num} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    with open(path, "wb") as f:
        f.write(out)


def make_docx(path: str, paragraphs: list[str], table: list[list[str]] | None = None) -> None:
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                t.cell(r, c).text = value
    d.save(path)


def write(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


@pytest.fixture
def embedder():
    return HashEmbedder()


@pytest.fixture
def store(tmp_path, embedder):
    s = Store(str(tmp_path / "index.db"), embedder.name, embedder.dim)
    yield s
    s.close()
