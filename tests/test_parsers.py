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


PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Saving seeds | Garden Blog</title>
<style>p { color: red }</style><script>track();</script></head>
<body>
<header><a href="/">Garden Blog</a><nav><a href="/about">About</a> <a href="/shop">Shop</a></nav></header>
<div class="cookie" role="dialog">We use cookies. <button>OK</button></div>
<main>
  <article>
    <header><h1>Saving seeds</h1><p class="byline">By Ann</p></header>
    <p>Dry the   seeds
       for a week.</p>
    <h2>Storing them</h2>
    <p>Keep them in a <a href="jars.html#glass">glass jar</a> &amp; out of the light.</p>
    <table><tr><th>Seed</th><th>Years</th></tr><tr><td>Tomato</td><td>4</td></tr></table>
    <pre>line one
line two</pre>
    <div hidden>Secret text</div>
    <form><input name="q"><button>Search</button></form>
  </article>
  <aside>Related posts</aside>
</main>
<footer>Copyright 2026 Garden Blog</footer>
</body></html>"""


def test_html_keeps_the_article_and_drops_page_furniture(tmp_path):
    path = write(str(tmp_path / "seeds.html"), PAGE)
    sections = parse(path)
    assert [(s.text, s.heading) for s in sections] == [
        ("Saving seeds", "Saving seeds"),
        ("By Ann", "Saving seeds"),
        ("Dry the seeds for a week.", "Saving seeds"),
        ("Storing them", "Saving seeds > Storing them"),
        ("Keep them in a glass jar & out of the light.", "Saving seeds > Storing them"),
        ("Seed | Years", "Saving seeds > Storing them"),
        ("Tomato | 4", "Saving seeds > Storing them"),
        ("line one\nline two", "Saving seeds > Storing them"),
        ("Related posts", "Saving seeds > Storing them"),
    ]
    assert [s.location for s in sections] == [f"paragraph {i}" for i in range(1, 10)]
    assert sections[4].links == (("jars.html#glass", "glass jar"),)


def test_html_without_landmarks_uses_body_and_title(tmp_path):
    page = ("<html><head><title>Meeting notes</title></head><body>"
            "<header>Intranet</header><p>Budget approved.<br>Next meeting Friday.</p>"
            "<h2>Actions</h2><ul><li>Book room<li>Send agenda</ul>"
            "<footer>Page 1</footer></body></html>")
    path = write(str(tmp_path / "notes.htm"), page)
    assert [(s.text, s.heading) for s in parse(path)] == [
        ("Budget approved.\nNext meeting Friday.", "Meeting notes"),
        ("Actions", "Meeting notes > Actions"),
        ("Book room", "Meeting notes > Actions"),
        ("Send agenda", "Meeting notes > Actions"),
    ]


def test_html_fragment_and_articles(tmp_path):
    path = write(str(tmp_path / "frag.html"), "Just some text <b>here</b>.")
    assert [(s.text, s.heading) for s in parse(path)] == [("Just some text here.", "")]
    page = ("<body><nav>Menu</nav><div class='side'>Ads</div>"
            "<article><h1>One</h1><p>First post.</p></article>"
            "<article><h1>Two</h1><p>Second post.</p></article></body>")
    path = write(str(tmp_path / "posts.html"), page)
    assert [(s.text, s.heading) for s in parse(path)] == [
        ("One", "One"), ("First post.", "One"), ("Two", "Two"), ("Second post.", "Two")]


def test_html_text_encodings(tmp_path):
    path = tmp_path / "old.html"
    path.write_bytes('<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252">'
                     '</head><body><p>Caf\u00e9 \u201cmenu\u201d</p></body></html>'.encode("cp1252"))
    assert parse(str(path))[0].text == "Café “menu”"
    path = tmp_path / "guess.htm"
    path.write_bytes("<p>Na\xefve</p>".encode("cp1252"))
    assert parse(str(path))[0].text == "Naïve"
    path = tmp_path / "bom.html"
    path.write_bytes(b"\xef\xbb\xbf" + "<p>© 2026</p>".encode("utf-8"))
    assert parse(str(path))[0].text == "© 2026"


def test_html_links_to_local_files(tmp_path):
    from collection_search.links import extract

    path = write(str(tmp_path / "site" / "index.html"),
                 '<p>See <a href="guide/setup.html#top">the setup guide</a>, '
                 '<a href="https://example.com">a site</a> and <a href="#later">below</a>.</p>')
    links = extract(path, parse(path))
    assert [(l.kind, l.label) for l in links] == [("link", "the setup guide")]
    assert links[0].target.lower().endswith("setup.html")


def test_html_furniture_is_kept_when_it_is_most_of_the_page(tmp_path):
    page = ("<body><div class='site-nav'>Home | About</div><div id='cookie-banner'>We use cookies</div>"
            "<h2>Notes<a class='headerlink' href='#notes'>¶</a></h2><p class='hidden-xs'>Shown on desktop.</p>"
            "<p class='sr-only'>Skip</p><div class='related-posts'>Other posts</div></body>")
    path = write(str(tmp_path / "page.html"), page)
    assert [(s.text, s.heading) for s in parse(path)] == [("Notes", "Notes"), ("Shown on desktop.", "Notes")]
    # An old page wrapped whole in a <form>, and a page that is only a list of links.
    path = write(str(tmp_path / "form.htm"), "<body><form><h1>Policy</h1><p>All staff must sign in.</p>"
                 "<input type='submit'></form></body>")
    assert [s.text for s in parse(path)] == ["Policy", "All staff must sign in."]
    path = write(str(tmp_path / "links.html"), "<nav><ul><li><a href='a.html'>Chapter A</a>"
                 "<li><a href='b.html'>Chapter B</a></ul></nav>")
    assert [s.text for s in parse(path)] == ["Chapter A", "Chapter B"]
