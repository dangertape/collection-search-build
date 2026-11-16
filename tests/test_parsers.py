import pytest

from collection_search.parsers import ParseError, parse
from collection_search.parsers.pdf_parser import count_empty_pages

from conftest import make_docx, make_epub, make_pdf, write


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


def test_epub_chapters_become_headings_in_reading_order(tmp_path):
    path = str(tmp_path / "book.epub")
    make_epub(path, [
        ("ch1.xhtml", "Chapter 1: Beginnings",
         "<h1>Chapter 1</h1><h2>Beginnings</h2><p>In the   beginning\nwas the word.</p>"
         "<h2>Light</h2><p>Let there be <em>light</em>.</p>"),
        ("ch1b.xhtml", None, "<p>Still chapter one, in a second file.</p>"),
        ("ch2.xhtml", "Chapter 2: The Garden", "<h1>Chapter 2: The Garden</h1><p>A garden &amp; a river.</p>"
         "<p>Line one<br/>Line two</p><script>ignored()</script>"),
    ])
    sections = parse(path)
    assert [(s.text, s.heading) for s in sections] == [
        ("In the beginning was the word.", "Chapter 1: Beginnings"),
        ("Light", "Chapter 1: Beginnings > Light"),
        ("Let there be light.", "Chapter 1: Beginnings > Light"),
        ("Still chapter one, in a second file.", "Chapter 1: Beginnings"),
        ("A garden & a river.", "Chapter 2: The Garden"),
        ("Line one\nLine two", "Chapter 2: The Garden"),
    ]
    assert [s.location for s in sections] == [f"paragraph {i}" for i in range(1, 7)]
    assert "Contents" not in [s.text for s in sections]  # the table of contents page is skipped


def test_epub2_table_of_contents(tmp_path):
    path = str(tmp_path / "old.epub")
    make_epub(path, [("a.xhtml", "Preface", "<p>Why this book.</p>"),
                     ("b.xhtml", "Part One", "<div><h3>Notes</h3>Loose text</div>")], toc="ncx")
    assert [(s.text, s.heading) for s in parse(path)] == [
        ("Why this book.", "Preface"),
        ("Notes", "Part One > Notes"),
        ("Loose text", "Part One > Notes"),
    ]


def test_epub_without_table_of_contents_uses_headings(tmp_path):
    path = str(tmp_path / "plain.epub")
    make_epub(path, [("a.xhtml", None, "<h1>Psalm 23</h1><p>The Lord is my shepherd.</p>")], toc="none")
    sections = parse(path)
    assert [(s.text, s.heading) for s in sections] == [
        ("Psalm 23", "Psalm 23"),
        ("The Lord is my shepherd.", "Psalm 23"),
    ]


def test_epub_with_drm_raises_parse_error(tmp_path):
    path = str(tmp_path / "locked.epub")
    make_epub(path, [("a.xhtml", "One", "<p>secret</p>")], encrypted=["a.xhtml"])
    with pytest.raises(ParseError, match="DRM"):
        parse(path)
    bad = tmp_path / "broken.epub"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ParseError):
        parse(str(bad))
