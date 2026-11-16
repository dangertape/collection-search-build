"""EPUB e-books: the chapters in reading order, each chapter a heading.

An .epub is a zip of XHTML files. META-INF/container.xml names the package
(.opf) file, whose spine lists the chapter files in reading order. The table
of contents (EPUB 3 nav document, or the EPUB 2 toc.ncx) gives each chapter
its title. That title becomes the top heading of every paragraph in the
chapter, with any headings inside the chapter below it, so a result reads like
"Chapter 3: The Flood > Aftermath". Files not named in the table of contents
carry on under the chapter before them, since books often split one chapter
across several files.

Paragraphs are numbered through the whole book, like Word paragraphs, so
locations stay "paragraph N" and read_document can find them again.

Only the standard library is used, so the build needs no new packages.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from html.parser import HTMLParser
from urllib.parse import unquote
from xml.etree import ElementTree

from . import ParseError, Section

BLOCKS = {
    "p", "div", "li", "blockquote", "pre", "dd", "dt", "figcaption", "caption",
    "td", "th", "tr", "section", "article", "aside", "header", "footer",
    "table", "ul", "ol", "dl", "hr", "body",
    "h1", "h2", "h3", "h4", "h5", "h6",
}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
SKIP = {"head", "script", "style", "title", "svg", "math"}
# Fonts may be "encrypted" only to stop reuse; that is not DRM.
FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}


def parse(path: str) -> list[Section]:
    with zipfile.ZipFile(path) as book:
        names = set(book.namelist())
        opf_path = _package_path(book)
        opf = _xml(book, opf_path)
        base = posixpath.dirname(opf_path)
        manifest = {}
        for item in _children(opf, "manifest", "item"):
            href = item.get("href")
            if item.get("id") and href:
                manifest[item.get("id")] = (_join(base, href), item.get("media-type", ""), item.get("properties", ""))

        spine = next(iter(_children(opf, "spine")), None)
        spine_files = []
        nav_files = set()
        for ref in _children(opf, "spine", "itemref"):
            entry = manifest.get(ref.get("idref"))
            if entry and entry[0] in names:
                spine_files.append(entry[0])
        for href, media, props in manifest.values():
            if "nav" in props.split():
                nav_files.add(href)

        _refuse_drm(book, names, set(spine_files))
        titles = _toc_titles(book, names, manifest, spine, nav_files)

        sections: list[Section] = []
        chapter = ""
        number = 0
        for href in spine_files:
            if href in nav_files:
                continue  # the table of contents page itself
            chapter = titles.get(href, chapter)
            raw = book.read(href).decode("utf-8", errors="replace")
            trail: list[tuple[int, str]] = [(0, chapter)] if chapter else []
            opening = True  # still in the headings at the top of the file
            for text, level in _blocks(raw):
                if level:
                    if opening and _part_of(text, chapter):
                        continue  # the chapter's own title, already the top heading
                    trail = [t for t in trail if t[0] < level] + [(level, text)]
                else:
                    opening = False
                number += 1
                sections.append(Section(text, f"paragraph {number}", " > ".join(t for _, t in trail)))
    return sections


# Package and table of contents --------------------------------------------

def _package_path(book: zipfile.ZipFile) -> str:
    try:
        container = _xml(book, "META-INF/container.xml")
    except KeyError:
        container = None
    if container is not None:
        for el in container.iter():
            if _local(el.tag) == "rootfile" and el.get("full-path"):
                return el.get("full-path")
    opfs = [n for n in book.namelist() if n.lower().endswith(".opf")]
    if not opfs:
        raise ParseError("Not an e-book: no package (.opf) file")
    return opfs[0]


def _toc_titles(book, names, manifest, spine, nav_files) -> dict[str, str]:
    """Chapter file -> title, from the EPUB 3 nav document or else toc.ncx."""
    titles: dict[str, str] = {}
    for nav in sorted(nav_files):
        if nav in names:
            _nav_titles(book.read(nav).decode("utf-8", errors="replace"), posixpath.dirname(nav), titles)
    if titles:
        return titles
    ncx = None
    if spine is not None and spine.get("toc") in manifest:
        ncx = manifest[spine.get("toc")][0]
    else:
        ncx = next((h for h, media, _ in manifest.values() if media == "application/x-dtbncx+xml"), None)
    if ncx and ncx in names:
        root = _xml(book, ncx)
        for point in root.iter():
            if _local(point.tag) != "navPoint":
                continue
            label = content = None
            for child in point:
                if _local(child.tag) == "navLabel":
                    label = " ".join("".join(child.itertext()).split())
                elif _local(child.tag) == "content":
                    content = child.get("src")
            if label and content:
                titles.setdefault(_join(posixpath.dirname(ncx), content), label)
    return titles


def _nav_titles(html: str, base: str, titles: dict[str, str]) -> None:
    parser = _NavParser()
    parser.feed(html)
    parser.close()
    for href, label in parser.links:
        if label and not href.startswith(("http:", "https:", "mailto:")):
            titles.setdefault(_join(base, href), label)


class _NavParser(HTMLParser):
    """Links inside <nav epub:type="toc">, or every link if there is no such nav."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._all: list[tuple[str, str]] = []
        self._nav_depth = 0
        self._in_toc = False
        self._href = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            self._nav_depth += 1
            kind = attrs.get("epub:type", "") or attrs.get("role", "")
            if "toc" in kind.split() or kind == "doc-toc":
                self._in_toc = True
        elif tag == "a" and attrs.get("href"):
            self._href = attrs["href"]
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            link = (self._href, " ".join("".join(self._text).split()))
            self._all.append(link)
            if self._in_toc:
                self.links.append(link)
            self._href = None
        elif tag == "nav" and self._nav_depth:
            self._nav_depth -= 1
            if not self._nav_depth:
                self._in_toc = False

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def close(self):
        super().close()
        if not self.links:
            self.links = self._all


# Chapter text --------------------------------------------------------------

def _blocks(html: str) -> list[tuple[str, int]]:
    """(text, heading level or 0) for each block of text, in order."""
    parser = _TextParser()
    parser.feed(html)
    parser.close()
    return parser.blocks


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, int]] = []
        self._buf: list[str] = []
        self._skip = 0
        self._level = 0

    def _flush(self):
        lines = [" ".join(line.split()) for line in "".join(self._buf).split("\n")]
        text = "\n".join(line for line in lines if line)
        if text:
            self.blocks.append((text, self._level))
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self._skip += 1
        elif self._skip:
            return
        elif tag == "br":
            self._buf.append("\n")
        elif tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt.strip() and self._level:
                self._buf.append(alt)  # headings that are pictures of text
        elif tag in BLOCKS:
            self._flush()
            if tag in HEADINGS:
                self._level = HEADINGS[tag]

    def handle_startendtag(self, tag, attrs):
        if tag in SKIP:
            return
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in SKIP:
            self._skip = max(0, self._skip - 1)
        elif self._skip:
            return
        elif tag in BLOCKS:
            self._flush()
            if tag in HEADINGS:
                self._level = 0

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data.replace("\n", " "))

    def close(self):
        super().close()
        self._flush()


# Helpers -------------------------------------------------------------------

def _refuse_drm(book, names, spine_files) -> None:
    if "META-INF/encryption.xml" not in names:
        return
    root = _xml(book, "META-INF/encryption.xml")
    for data in root.iter():
        if _local(data.tag) != "EncryptedData":
            continue
        method = next((el.get("Algorithm") for el in data.iter() if _local(el.tag) == "EncryptionMethod"), "")
        uri = next((el.get("URI") for el in data.iter() if _local(el.tag) == "CipherReference"), "")
        if method not in FONT_OBFUSCATION and _join("", uri or "") in spine_files:
            raise ParseError("The e-book is DRM-protected, so its text can't be read")


def _xml(book: zipfile.ZipFile, name: str):
    return ElementTree.fromstring(book.read(name))


def _children(root, *path: str):
    """Elements at a path of local names below root, ignoring namespaces."""
    level = [root]
    for name in path:
        level = [child for el in level for child in el if _local(child.tag) == name]
    return level


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _join(base: str, href: str) -> str:
    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(base, href)) if base else posixpath.normpath(href)


def _part_of(heading: str, title: str) -> bool:
    """Is the heading the chapter title, or a piece of it ("Chapter 1" of "Chapter 1: The Flood")?"""
    norm = lambda s: " " + " ".join(re.sub(r"\W+", " ", s).casefold().split()) + " "
    return bool(title.strip()) and norm(heading).strip() != "" and norm(heading) in norm(title)
