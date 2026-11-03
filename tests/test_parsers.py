import pytest

from collection_search.parsers import ParseError, parse
from collection_search.parsers.pdf_parser import count_empty_pages

from conftest import make_docx, make_pdf, write


def test_text_and_markdown_split_into_paragraphs(tmp_path):
    path = write(str(tmp_path / "notes.md"), "# Title\n\nFirst para.\r\n\r\nSecond para.\n")
    sections = parse(path)
    assert [s.text for s in sections] == ["# Title", "First para.", "Second para."]
    assert sections[2].location == "paragraph 3"


def test_text_falls_back_from_utf8(tmp_path):
    path = tmp_path / "old.txt"
    path.write_bytes("Caf\xe9 menu".encode("cp1252"))
    assert parse(str(path))[0].text == "Café menu"


def test_pdf_one_section_per_page_and_skips_empty_pages(tmp_path):
    path = str(tmp_path / "doc.pdf")
    make_pdf(path, ["Lease agreement page one", "", "Rent is due monthly"])
    sections = parse(path)
    assert [(s.location, s.text) for s in sections] == [
        ("page 1", "Lease agreement page one"),
        ("page 3", "Rent is due monthly"),
    ]
    assert count_empty_pages(path) == 1


def test_docx_paragraphs_and_tables_in_order(tmp_path):
    path = str(tmp_path / "contract.docx")
    make_docx(path, ["Intro", "", "Terms apply"], table=[["Item", "Price"], ["Widget", "42"]])
    sections = parse(path)
    assert [s.text for s in sections] == ["Intro", "Terms apply", "Item | Price", "Widget | 42"]
    assert sections[3].location == "paragraph 4 (table)"


def test_unsupported_and_broken_files_raise_parse_error(tmp_path):
    with pytest.raises(ParseError):
        parse(write(str(tmp_path / "x.xyz"), "hi"))
    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    with pytest.raises(ParseError):
        parse(str(bad))
