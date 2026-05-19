from __future__ import annotations
import json
import logging
import re
from typing import Optional, List

from lenny.catalog.types import BookMetadata

logger = logging.getLogger(__name__)

_ISBN13_RE = re.compile(r'97[89]\d{10}')
_ISBN10_RE = re.compile(r'\d{9}[\dX]')


def extract_epub(epub_path: str) -> BookMetadata:
    """Extract BookMetadata from an EPUB file by reading its OPF container."""
    from ebooklib import epub  # local import — worker only, keeps API startup fast

    book = epub.read_epub(epub_path, options={"ignore_ncx": True})

    def _first(meta_list) -> Optional[str]:
        for item in (meta_list or []):
            val = item[0] if isinstance(item, tuple) else item
            if val and str(val).strip():
                return str(val).strip()
        return None

    title = _first(book.get_metadata('DC', 'title'))
    authors = [
        str(a[0]).strip()
        for a in (book.get_metadata('DC', 'creator') or [])
        if a and a[0]
    ]
    publisher = _first(book.get_metadata('DC', 'publisher'))
    language = _first(book.get_metadata('DC', 'language'))
    description = _first(book.get_metadata('DC', 'description'))
    publish_date = _first(book.get_metadata('DC', 'date'))
    if publish_date:
        m = re.match(r'(\d{4})', publish_date)
        publish_date = m.group(1) if m else publish_date

    subjects = [
        str(s[0]).strip()
        for s in (book.get_metadata('DC', 'subject') or [])
        if s and s[0]
    ]

    isbn_13: Optional[str] = None
    isbn_10: Optional[str] = None
    for ident_tuple in (book.get_metadata('DC', 'identifier') or []):
        raw = str(ident_tuple[0]).strip() if ident_tuple else ""
        clean = re.sub(r'^(?:urn:isbn:|isbn:)', '', raw, flags=re.IGNORECASE).replace('-', '').strip()
        if _ISBN13_RE.fullmatch(clean):
            isbn_13 = clean
        elif _ISBN10_RE.fullmatch(clean):
            isbn_10 = clean

    return BookMetadata(
        title=title,
        authors=authors,
        isbn_13=isbn_13,
        isbn_10=isbn_10,
        publisher=publisher,
        publish_date=publish_date,
        language=language,
        description=description,
        subjects=subjects,
        source="epub_opf",
    )


def extract_json_sidecar(json_path: str) -> BookMetadata:
    """Extract BookMetadata from a JSON sidecar file."""
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    authors: List[str] = []
    if isinstance(data.get("authors"), list):
        authors = [str(a) for a in data["authors"] if a]
    elif data.get("author"):
        authors = [str(data["author"])]

    isbn_13 = data.get("isbn_13") or data.get("isbn13")
    isbn_10 = data.get("isbn_10") or data.get("isbn10")
    if not isbn_13 and not isbn_10 and data.get("isbn"):
        raw = str(data["isbn"]).replace("-", "").strip()
        if len(raw) == 13:
            isbn_13 = raw
        elif len(raw) == 10:
            isbn_10 = raw

    # Validate ISBN format
    if isbn_13 and not _ISBN13_RE.fullmatch(isbn_13.replace('-', '')):
        isbn_13 = None
    if isbn_10 and not _ISBN10_RE.fullmatch(isbn_10.replace('-', '')):
        isbn_10 = None

    subjects = data.get("subjects", []) or []
    if isinstance(subjects, str):
        subjects = [subjects]
    elif not isinstance(subjects, list):
        subjects = [str(subjects)]

    publish_date = data.get("publish_date") or data.get("year")
    if publish_date:
        m = re.match(r'(\d{4})', str(publish_date))
        publish_date = m.group(1) if m else publish_date

    return BookMetadata(
        title=data.get("title"),
        authors=authors,
        isbn_13=isbn_13,
        isbn_10=isbn_10,
        publisher=data.get("publisher"),
        publish_date=publish_date,
        language=data.get("language"),
        description=data.get("description"),
        subjects=subjects,
        source="json_sidecar",
    )


def extract_csv_row(row: dict) -> BookMetadata:
    """Extract BookMetadata from a CSV row dict."""
    def _get(*keys) -> Optional[str]:
        for k in keys:
            v = row.get(k) or row.get(k.upper()) or row.get(k.lower())
            if v and str(v).strip():
                return str(v).strip()
        return None

    title = _get("title")

    authors: List[str] = []
    raw_authors = _get("authors", "author")
    if raw_authors:
        parts = re.split(r'[;|]', raw_authors)
        authors = [p.strip() for p in parts if p.strip()]

    isbn_13: Optional[str] = None
    isbn_10: Optional[str] = None
    raw_isbn = _get("isbn_13", "isbn13")
    if raw_isbn:
        isbn_13 = raw_isbn.replace("-", "").strip()
    raw_isbn10 = _get("isbn_10", "isbn10")
    if raw_isbn10:
        isbn_10 = raw_isbn10.replace("-", "").strip()
    if not isbn_13 and not isbn_10:
        generic = _get("isbn")
        if generic:
            clean = generic.replace("-", "").strip()
            if len(clean) == 13:
                isbn_13 = clean
            elif len(clean) == 10:
                isbn_10 = clean

    # Validate ISBN format
    if isbn_13 and not _ISBN13_RE.fullmatch(isbn_13.replace('-', '')):
        isbn_13 = None
    if isbn_10 and not _ISBN10_RE.fullmatch(isbn_10.replace('-', '')):
        isbn_10 = None

    publish_date = _get("publish_date", "year", "date")
    if publish_date:
        m = re.match(r'(\d{4})', str(publish_date))
        publish_date = m.group(1) if m else publish_date

    return BookMetadata(
        title=title,
        authors=authors,
        isbn_13=isbn_13,
        isbn_10=isbn_10,
        publisher=_get("publisher"),
        publish_date=publish_date,
        language=_get("language"),
        description=_get("description"),
        subjects=[],
        source="csv",
    )
