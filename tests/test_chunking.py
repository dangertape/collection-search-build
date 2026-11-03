import pytest

from collection_search.chunking import chunk_sections
from collection_search.parsers import Section


def para(n, words=30):
    return " ".join(f"p{n}w{i}" for i in range(words))


def test_short_sections_merge_into_one_chunk():
    chunks = chunk_sections([Section("one", "paragraph 1"), Section("two", "paragraph 2")])
    assert len(chunks) == 1
    assert chunks[0].text == "one\n\ntwo"
    assert chunks[0].location == "paragraphs 1-2"


def test_chunks_respect_size_and_overlap():
    sections = [Section(para(i), f"page {i}") for i in range(1, 21)]
    chunks = chunk_sections(sections, size=400, overlap=80)
    assert len(chunks) > 3
    assert all(len(c.text) <= 400 + 80 for c in chunks)
    for prev, nxt in zip(chunks, chunks[1:]):
        first_words = nxt.text.split()[:3]
        assert " ".join(first_words) in prev.text  # next chunk starts with the end of the previous


def test_every_word_is_kept():
    sections = [Section(para(i), f"page {i}") for i in range(1, 11)]
    words = {w for s in sections for w in s.text.split()}
    chunked = {w for c in chunk_sections(sections, size=300, overlap=50) for w in c.text.split()}
    assert words <= chunked


def test_long_paragraph_is_split():
    long_text = ". ".join(para(1, 12) for _ in range(30)) + "."
    chunks = chunk_sections([Section(long_text, "page 7")], size=500, overlap=100)
    assert len(chunks) > 3
    assert all(c.location == "page 7" for c in chunks)
    assert all(len(c.text) <= 600 for c in chunks)


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        chunk_sections([Section("x", "page 1")], size=100, overlap=100)
