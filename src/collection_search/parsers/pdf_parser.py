from __future__ import annotations

from pypdf import PdfReader

from . import Section


def parse(path: str) -> list[Section]:
    """One section per page. Image-only pages yield no section."""
    reader = PdfReader(path)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            from . import ParseError

            raise ParseError("PDF is password protected") from exc
    sections = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            sections.append(Section(text, f"page {number}"))
    return sections


def count_empty_pages(path: str) -> int:
    """Pages with no extractable text, usually scans. Shown on the Status tab later."""
    reader = PdfReader(path)
    return sum(1 for page in reader.pages if not (page.extract_text() or "").strip())
