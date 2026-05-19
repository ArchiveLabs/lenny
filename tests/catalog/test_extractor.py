import os
import json
import tempfile
import pytest
from ebooklib import epub

from lenny.catalog.extractor import extract_epub, extract_json_sidecar, extract_csv_row
from lenny.catalog.types import BookMetadata


# --- Helpers ---

def make_test_epub(path: str, title: str = "Dune", author: str = "Frank Herbert",
                   isbn: str = None, publisher: str = None, language: str = "en",
                   description: str = None) -> str:
    """Write a minimal valid EPUB to path and return path."""
    book = epub.EpubBook()
    book.set_title(title)
    book.add_author(author)
    book.set_language(language)
    if isbn:
        book.set_identifier(isbn)
    if publisher:
        book.add_metadata('DC', 'publisher', publisher)
    if description:
        book.add_metadata('DC', 'description', description)
    c1 = epub.EpubHtml(title='Chapter 1', file_name='chap1.xhtml', lang='en')
    c1.content = b'<html><body><p>Test content</p></body></html>'
    book.add_item(c1)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ['nav', c1]
    epub.write_epub(path, book)
    return path


# --- extract_epub tests ---

def test_extract_epub_basic_fields(tmp_path):
    epub_path = make_test_epub(str(tmp_path / "dune.epub"), title="Dune", author="Frank Herbert")
    meta = extract_epub(epub_path)
    assert isinstance(meta, BookMetadata)
    assert meta.title == "Dune"
    assert "Frank Herbert" in meta.authors
    assert meta.source == "epub_opf"


def test_extract_epub_with_isbn(tmp_path):
    epub_path = make_test_epub(str(tmp_path / "book.epub"), isbn="9780441013593")
    meta = extract_epub(epub_path)
    assert meta.isbn_13 == "9780441013593"


def test_extract_epub_with_language(tmp_path):
    epub_path = make_test_epub(str(tmp_path / "book.epub"), language="fr")
    meta = extract_epub(epub_path)
    assert meta.language == "fr"


def test_extract_epub_with_publisher(tmp_path):
    epub_path = make_test_epub(str(tmp_path / "book.epub"), publisher="Chilton Books")
    meta = extract_epub(epub_path)
    assert meta.publisher == "Chilton Books"


def test_extract_epub_missing_file_raises():
    with pytest.raises(Exception):
        extract_epub("/nonexistent/path/book.epub")


def test_extract_epub_is_resolvable_with_title_and_author(tmp_path):
    epub_path = make_test_epub(str(tmp_path / "book.epub"), title="Dune", author="Frank Herbert")
    meta = extract_epub(epub_path)
    assert meta.is_resolvable is True


# --- extract_json_sidecar tests ---

def test_extract_json_sidecar_full(tmp_path):
    data = {
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "isbn_13": "9780441013593",
        "publisher": "Chilton Books",
        "publish_date": "1965",
        "language": "en",
    }
    json_path = str(tmp_path / "meta.json")
    with open(json_path, "w") as f:
        json.dump(data, f)
    meta = extract_json_sidecar(json_path)
    assert meta.title == "Dune"
    assert meta.isbn_13 == "9780441013593"
    assert meta.source == "json_sidecar"


def test_extract_json_sidecar_partial_fields(tmp_path):
    data = {"title": "Dune"}
    json_path = str(tmp_path / "meta.json")
    with open(json_path, "w") as f:
        json.dump(data, f)
    meta = extract_json_sidecar(json_path)
    assert meta.title == "Dune"
    assert meta.authors == []


def test_extract_json_sidecar_single_author_field(tmp_path):
    data = {"title": "Dune", "author": "Frank Herbert"}
    json_path = str(tmp_path / "meta.json")
    with open(json_path, "w") as f:
        json.dump(data, f)
    meta = extract_json_sidecar(json_path)
    assert "Frank Herbert" in meta.authors


def test_extract_json_sidecar_missing_file_raises():
    with pytest.raises(Exception):
        extract_json_sidecar("/nonexistent/meta.json")


# --- extract_csv_row tests ---

def test_extract_csv_row_basic():
    row = {"title": "Dune", "author": "Frank Herbert", "isbn": "9780441013593"}
    meta = extract_csv_row(row)
    assert meta.title == "Dune"
    assert "Frank Herbert" in meta.authors
    assert meta.source == "csv"


def test_extract_csv_row_multiple_authors():
    row = {"title": "Book", "authors": "Alice Smith; Bob Jones"}
    meta = extract_csv_row(row)
    assert len(meta.authors) == 2


def test_extract_csv_row_pipe_separated_authors():
    row = {"title": "Book", "authors": "Alice Smith|Bob Jones"}
    meta = extract_csv_row(row)
    assert len(meta.authors) == 2


def test_extract_csv_row_isbn13_column():
    row = {"title": "Book", "author": "Author", "isbn_13": "9780441013593"}
    meta = extract_csv_row(row)
    assert meta.isbn_13 == "9780441013593"


def test_extract_csv_row_empty_row():
    meta = extract_csv_row({})
    assert meta.title is None
    assert meta.authors == []
