"""Web pages (.html, .htm): the readable text, each paragraph under its headings.

A saved web page is mostly not the article: menus, cookie banners, sidebars,
footers, scripts and styles. Those are left out so search, the Similar tab and
the Map see what the page is about:
  - script, style, noscript, template, buttons and anything marked hidden
    never count;
  - nav, form, dialog and elements with a role such as navigation, banner or
    search, or a class or id such as "sidebar", "cookie" or "breadcrumb", are
    left out too, unless one holds most of the page's text (a page that is
    just a list of links, or one wrapped whole in a <form>);
  - when the page marks its content with <main> (or role="main"), only that is
    read; otherwise, if it has <article> elements, only those are read;
    otherwise the whole <body> is read without its page-level header, footer
    and aside.

Each paragraph carries the headings above it ("Install > On Windows"), like
Markdown. A page without an <h1> uses its <title> as the top heading instead.
Table rows read as "cell | cell", like Word tables. Links to other local files
are kept so the Links view and get_document_links can follow them.

Locations are "paragraph N", so read_document can find them again. Only the
standard library is used. The EPUB parser reads its chapters with the same
code (blocks()), without the boilerplate removal.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from . import Section

BLOCKS = {
    "p", "div", "li", "blockquote", "pre", "dd", "dt", "figcaption", "caption",
    "tr", "section", "article", "aside", "header", "footer", "main", "figure",
    "table", "ul", "ol", "dl", "hr", "body", "address", "details", "summary",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
CELLS = {"td", "th"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
# Never text, in any document.
SKIP = {"head", "script", "style", "title", "svg", "math", "template", "noscript"}
# Page furniture rather than content, left out of web pages.
CONTROLS = {"button", "select", "textarea", "iframe", "object"}
HIDDEN_CLASSES = {"hidden", "visually-hidden", "sr-only", "screen-reader-text", "d-none", "mw-hidden-catlinks"}
# Page furniture rather than content: left out of web pages unless it holds most of the text.
FURNITURE = {"nav", "form", "dialog", "menu"}
FURNITURE_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "menu",
                   "menubar", "dialog", "alertdialog", "toolbar"}
# Words in a class or id that mark furniture, e.g. "site-nav" or "cookie_banner".
FURNITURE_WORDS = {
    "nav", "navbar", "navigation", "menu", "sidebar", "breadcrumb", "breadcrumbs", "cookie", "cookies",
    "consent", "banner", "dropdown", "toc", "share", "sharing", "social", "related", "advert", "ads",
    "advertisement", "sponsored", "popup", "modal", "newsletter", "subscribe", "catlinks", "noprint",
    "headerlink", "editsection", "printfooter", "skiplink", "comments",
}
NEVER_FURNITURE = {"html", "body", "main", "article"}
FURNITURE_SHARE = 0.5  # furniture holding more of the text than this is kept
CHROME = {"header", "footer", "aside"}  # left out when they belong to the page rather than an article


@dataclass
class Block:
    text: str
    level: int = 0  # heading level, 0 for ordinary text
    where: frozenset = frozenset()  # "main", "article" and/or "chrome": what the block sits inside
    furniture: frozenset = frozenset()  # ids of the furniture elements it sits inside
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, link text)


def parse(path: str) -> list[Section]:
    with open(path, "rb") as f:
        raw = f.read()
    parser = _Parser(boilerplate=True)
    parser.feed(_decode(raw))
    parser.close()

    blocks = _content(parser.blocks)

    title = parser.title
    has_h1 = any(b.level == 1 for b in blocks)
    trail: list[tuple[int, str]] = [(0, title)] if title and not has_h1 else []
    sections = []
    for number, block in enumerate(blocks, start=1):
        if block.level:
            trail = [t for t in trail if t[0] < block.level] + [(block.level, block.text)]
        sections.append(Section(block.text, f"paragraph {number}", " > ".join(t for _, t in trail),
                                tuple(block.links)))
    return sections


def _content(blocks: list[Block]) -> list[Block]:
    """The blocks that make up the page's content."""
    if any("main" in b.where for b in blocks):
        blocks = [b for b in blocks if "main" in b.where]
    elif any("article" in b.where for b in blocks):
        blocks = [b for b in blocks if "article" in b.where]
    else:
        blocks = [b for b in blocks if "chrome" not in b.where]
    total = sum(len(b.text) for b in blocks)
    size: dict[int, int] = {}
    for b in blocks:
        for f in b.furniture:
            size[f] = size.get(f, 0) + len(b.text)
    small = {f for f, n in size.items() if n <= total * FURNITURE_SHARE}
    kept = [b for b in blocks if not (b.furniture & small)]
    return kept or blocks


def blocks(html: str) -> list[tuple[str, int]]:
    """(text, heading level or 0) for each block of text in an HTML document, everything included."""
    parser = _Parser(boilerplate=False)
    parser.feed(html)
    parser.close()
    return [(b.text, b.level) for b in parser.blocks]


# Text encoding -------------------------------------------------------------

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:\-]+)""", re.IGNORECASE)


def _decode(raw: bytes) -> str:
    """The page as text, going by its byte-order mark, then its <meta charset>, then guessing."""
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace")
    m = _META_CHARSET.search(raw[:4096])
    if m:
        try:
            name = codecs.lookup(m.group(1).decode("ascii")).name
            if name.startswith("utf-16"):  # a page saved as bytes can't really be UTF-16 here
                name = "utf-8"
            if name in ("latin-1", "iso8859-1", "ascii"):
                name = "cp1252"  # what browsers do with these labels
            return raw.decode(name, errors="replace")
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


# Blocks of text ------------------------------------------------------------

@dataclass
class _Region:
    tag: str
    kind: str  # "skip", "main", "article", "chrome" or "furniture"
    id: int = 0
    depth: int = 1  # open elements with this tag, counting the region's own


class _Parser(HTMLParser):
    def __init__(self, boilerplate: bool):
        super().__init__(convert_charrefs=True)
        self.boilerplate = boilerplate
        self.blocks: list[Block] = []
        self.title = ""
        self._title: list[str] | None = None
        self._buf: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._link: tuple[str, list[str]] | None = None
        self._regions: list[_Region] = []
        self._ids = 0
        self._level = 0
        self._pre = 0
        self._cells = 0  # cells started in the current table row

    # Regions ---------------------------------------------------------------
    def _skipping(self) -> bool:
        return any(r.kind == "skip" for r in self._regions)

    def _where(self) -> frozenset:
        return frozenset(r.kind for r in self._regions if r.kind not in ("skip", "furniture"))

    def _furniture(self) -> frozenset:
        return frozenset(r.id for r in self._regions if r.kind == "furniture")

    def _region_kind(self, tag: str, attrs: dict) -> str | None:
        if tag in SKIP:
            return "skip"
        if not self.boilerplate:
            return None
        role = (attrs.get("role") or "").lower()
        classes = attrs.get("class", "").lower().split()
        if (tag in CONTROLS or "hidden" in attrs or (attrs.get("aria-hidden") or "").lower() == "true"
                or HIDDEN_CLASSES.intersection(classes)):
            return "skip"
        if tag == "main" or role == "main":
            return "main"
        if tag == "article" or role == "article":
            return "article"
        if tag in FURNITURE or role in FURNITURE_ROLES:
            return "furniture"
        if tag not in NEVER_FURNITURE:
            words = re.split(r"[^a-z0-9]+", " ".join(classes) + " " + attrs.get("id", "").lower())
            if FURNITURE_WORDS.intersection(words):
                return "furniture"
        if tag in CHROME and not any(r.kind in ("main", "article") for r in self._regions):
            return "chrome"
        return None

    # Parser events ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        attrs = {k: v or "" for k, v in attrs}
        if tag == "title" and self._title is None and not self.title:
            self._title = []
        if tag not in VOID:
            for region in self._regions:
                if region.tag == tag:
                    region.depth += 1
        kind = self._region_kind(tag, attrs)
        if kind and tag not in VOID:
            if kind != "skip" and not self._skipping():
                self._flush()
            self._ids += 1
            self._regions.append(_Region(tag, kind, id=self._ids))
            if kind == "skip":
                return
        if self._skipping():
            return
        if tag == "br":
            self._buf.append("\n")
        elif tag == "img":
            alt = attrs.get("alt", "")
            if alt.strip() and self._level:
                self._buf.append(alt)  # headings that are pictures of text
        elif tag == "a" and attrs.get("href"):
            self._link = (attrs["href"], [])
        elif tag in CELLS:
            if self._cells and "".join(self._buf).strip():
                self._buf.append(" | ")
            self._cells += 1
        elif tag in BLOCKS:
            self._flush()
            if tag in HEADINGS:
                self._level = HEADINGS[tag]
            elif tag == "pre":
                self._pre += 1
            elif tag == "tr":
                self._cells = 0

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title" and self._title is not None:
            self.title = " ".join("".join(self._title).split())
            self._title = None
        skipping = self._skipping()
        closed = False
        for region in self._regions:
            if region.tag == tag:
                region.depth -= 1
                closed = closed or region.depth == 0
        if closed:
            if not skipping:
                self._flush()
            self._regions = [r for r in self._regions if r.depth > 0]
            if skipping:
                return
        if skipping:
            return
        if tag == "a" and self._link is not None:
            href, text = self._link
            self._links.append((href, " ".join("".join(text).split())))
            self._link = None
        elif tag in BLOCKS:
            self._flush()
            if tag in HEADINGS:
                self._level = 0
            elif tag == "pre":
                self._pre = max(0, self._pre - 1)

    def handle_data(self, data):
        if self._title is not None:
            self._title.append(data)
        if self._skipping():
            return
        self._buf.append(data if self._pre else data.replace("\n", " "))
        if self._link is not None:
            self._link[1].append(data)

    def close(self):
        super().close()
        self._flush()

    def _flush(self):
        lines = [" ".join(line.split()) for line in "".join(self._buf).split("\n")]
        text = "\n".join(line for line in lines if line).strip(" |")
        if text:
            self.blocks.append(Block(text, self._level, self._where(), self._furniture(), self._links))
            self._links = []
        self._buf = []
