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


def make_epub(path: str, chapters: list[tuple[str, str | None, str]], toc: str = "nav",
              encrypted: list[str] = ()) -> None:
    """Write a small EPUB. chapters: (file name, table-of-contents title or None, body XHTML).

    toc is "nav" (EPUB 3), "ncx" (EPUB 2) or "none". Files are stored under OEBPS/.
    """
    import zipfile

    def page(body: str) -> str:
        return ('<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
                'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>x</title>'
                f'<style>p {{ margin: 0 }}</style></head><body>{body}</body></html>')

    listed = [(name, title) for name, title, _ in chapters if title]
    items = "".join(f'<item id="c{i}" href="{name}" media-type="application/xhtml+xml"/>'
                    for i, (name, _, _) in enumerate(chapters))
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(chapters)))
    files = {name: page(body) for name, _, body in chapters}
    spine_attr = ""
    if toc == "nav":
        links = "".join(f'<li><a href="{n}#start">{t}</a></li>' for n, t in listed)
        files["nav.xhtml"] = page(f'<nav epub:type="landmarks"><ol><li><a href="{chapters[0][0]}">Start</a></li></ol></nav>'
                                  f'<nav epub:type="toc"><h1>Contents</h1><ol>{links}</ol></nav>')
        items += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        spine = '<itemref idref="nav"/>' + spine
    elif toc == "ncx":
        points = "".join(f'<navPoint id="p{i}"><navLabel><text>{t}</text></navLabel><content src="{n}"/></navPoint>'
                         for i, (n, t) in enumerate(listed))
        files["toc.ncx"] = ('<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                            f'<navMap>{points}</navMap></ncx>')
        items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        spine_attr = ' toc="ncx"'
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
           f'<metadata/><manifest>{items}</manifest><spine{spine_attr}>{spine}</spine></package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        if encrypted:
            data = "".join('<enc:EncryptedData xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                           '<enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
                           f'<enc:CipherData><enc:CipherReference URI="OEBPS/{n}"/></enc:CipherData></enc:EncryptedData>'
                           for n in encrypted)
            z.writestr("META-INF/encryption.xml",
                       f'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">{data}</encryption>')
        z.writestr("OEBPS/content.opf", opf)
        for name, text in files.items():
            z.writestr(f"OEBPS/{name}", text)
