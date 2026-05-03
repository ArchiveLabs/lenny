import pytest
from unittest.mock import patch, MagicMock
import httpx

from lenny.catalog.resolver import APIResolver, OLResolver
from lenny.catalog.types import (
    BookMetadata, OLResult, OLStatus, ActionTaken,
)
from lenny.catalog.exceptions import OLRateLimited, OLAuthRequired, OLWriteError


# --- Protocol conformance ---

def test_api_resolver_satisfies_protocol():
    resolver = APIResolver()
    assert isinstance(resolver, OLResolver)


# --- ISBN lookup ---

def test_isbn_lookup_found(mock_ol_isbn_response):
    resolver = APIResolver()
    metadata = BookMetadata(title="Dune", authors=["Frank Herbert"], isbn_13="9780441013593")
    result = resolver.lookup(metadata)
    assert result.status == OLStatus.OL_MATCH_CLEAN
    assert result.olid == 7353218
    assert result.confidence >= 0.95
    assert result.action == ActionTaken.LINK_ONLY


def test_isbn_lookup_not_found():
    resolver = APIResolver()
    with patch("httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_resp
        )
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(title="Unknown Book", isbn_13="9780000000000")
        result = resolver.lookup(metadata)
    # Falls through to search — but with no mock for search, returns not found
    assert result.status in (OLStatus.OL_NOT_FOUND, OLStatus.INSUFFICIENT_METADATA)


def test_isbn_lookup_title_mismatch_falls_through():
    """ISBN found but title diverges >20% — treat as ISBN reuse, fall to search."""
    resolver = APIResolver()
    with patch.object(resolver, "_lookup_isbn") as mock_isbn:
        mock_isbn.return_value = OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)
        with patch.object(resolver, "_search_exact") as mock_search:
            mock_search.return_value = OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)
            metadata = BookMetadata(title="Completely Different Title", isbn_13="9780441013593")
            result = resolver.lookup(metadata)
    mock_isbn.assert_called_once()
    mock_search.assert_called_once()


def test_isbn_lookup_rate_limited_raises():
    resolver = APIResolver()
    with patch("httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429 Too Many Requests", request=MagicMock(), response=mock_resp
        )
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(isbn_13="9780441013593")
        with pytest.raises(OLRateLimited):
            resolver._lookup_isbn("9780441013593", metadata)


def test_insufficient_metadata_returns_immediately():
    resolver = APIResolver()
    metadata = BookMetadata()  # nothing set
    result = resolver.lookup(metadata)
    assert result.status == OLStatus.INSUFFICIENT_METADATA
    assert result.action == ActionTaken.NEEDS_REVIEW


@pytest.fixture
def mock_ol_isbn_response():
    mock_data = {
        "key": "/books/OL7353218M",
        "title": "Dune",
        "publishers": ["Chilton Books"],
        "publish_date": "1965",
    }
    with patch("httpx.Client") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = mock_data
        mock_resp.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        yield mock_resp


# --- create_edition ---

def test_create_edition_conflict_returns_existing_olid():
    """409 response with a parseable ID should return the existing OLID."""
    resolver = APIResolver(ol_session_cookie="valid-session")
    with patch.object(resolver, "_find_or_create_author", return_value="/authors/OL123A"):
        with patch("httpx.Client") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 409
            mock_resp.json.return_value = {"id": "/books/OL456M"}
            mock_resp.raise_for_status = MagicMock()
            mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            result = resolver.create_edition(BookMetadata(title="Book", authors=["Author"]))
    assert result == 456


def test_create_edition_conflict_missing_id_raises():
    """409 with no parseable ID in response body should raise OLWriteError."""
    resolver = APIResolver(ol_session_cookie="valid-session")
    with patch.object(resolver, "_find_or_create_author", return_value="/authors/OL123A"):
        with patch("httpx.Client") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 409
            mock_resp.json.return_value = {"error": "conflict"}  # no "id" field
            mock_resp.raise_for_status = MagicMock()
            mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            with pytest.raises(OLWriteError):
                resolver.create_edition(BookMetadata(title="Book", authors=["Author"]))


# --- OL search ---

def test_search_clean_match():
    resolver = APIResolver()
    search_data = {
        "docs": [{
            "title": "Dune",
            "author_name": ["Frank Herbert"],
            "editions": {"docs": [{"key": "/books/OL7353218M", "publish_date": "1965"}]},
        }]
    }
    with patch("httpx.Client") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = search_data
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(title="Dune", authors=["Frank Herbert"])
        result = resolver._search_exact(metadata)
    assert result.status == OLStatus.OL_MATCH_CLEAN
    assert result.olid == 7353218
    assert result.confidence >= 0.95


def test_search_fuzzy_match_goes_to_review():
    resolver = APIResolver()
    search_data = {
        "docs": [{
            "title": "Dune Messiah",
            "author_name": ["Frank Herbert"],
            "editions": {"docs": [{"key": "/books/OL9999M"}]},
        }]
    }
    with patch("httpx.Client") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = search_data
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(title="Dune", authors=["Frank Herbert"])
        result = resolver._search_exact(metadata)
    # "Dune" vs "Dune Messiah": title_score=0.5, author_score=1.0, combined=0.70
    # Exactly at OL_REVIEW_THRESHOLD — lands in fuzzy/review bucket
    assert result.status == OLStatus.OL_MATCH_FUZZY
    assert result.needs_review is True


def test_search_no_results_returns_not_found():
    resolver = APIResolver()
    with patch("httpx.Client") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"docs": []}
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(title="Zorp Unpublished", authors=["Nobody"])
        result = resolver._search_exact(metadata)
    assert result.status == OLStatus.OL_NOT_FOUND


def test_search_rate_limited_raises():
    resolver = APIResolver()
    with patch("httpx.Client") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=mock_resp
        )
        mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        with pytest.raises(OLRateLimited):
            resolver._search_exact(BookMetadata(title="Dune", authors=["Frank Herbert"]))


# --- Google Books ---

def test_google_books_found():
    resolver = APIResolver(google_books_api_key="test-key")
    gb_data = {
        "items": [{
            "volumeInfo": {
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "publishedDate": "1965",
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441013593"}],
            }
        }]
    }
    with patch("httpx.Client") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = gb_data
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(title="Dune", authors=["Frank Herbert"])
        result = resolver._google_books_lookup(metadata)
    assert result.action == ActionTaken.CREATE_FULL
    assert result.confidence >= 0.95


def test_google_books_no_api_key_skipped():
    resolver = APIResolver(google_books_api_key=None)
    metadata = BookMetadata(title="Dune", authors=["Frank Herbert"])
    with patch.object(resolver, "_google_books_lookup") as mock_gb:
        with patch.object(resolver, "_lookup_isbn", return_value=OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)):
            with patch.object(resolver, "_search_exact", return_value=OLResult(status=OLStatus.OL_NOT_FOUND, confidence=0.0)):
                resolver.lookup(metadata)
    mock_gb.assert_not_called()


def test_google_books_title_mismatch_ignored():
    resolver = APIResolver(google_books_api_key="test-key")
    gb_data = {"items": [{"volumeInfo": {"title": "Completely Different Book"}}]}
    with patch("httpx.Client") as mock_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = gb_data
        mock_resp.raise_for_status = MagicMock()
        mock_cls.return_value.__enter__.return_value.get.return_value = mock_resp
        metadata = BookMetadata(title="Dune", authors=["Frank Herbert"])
        result = resolver._google_books_lookup(metadata)
    assert result.status == OLStatus.OL_NOT_FOUND


# --- OL write: create_edition ---

def test_create_edition_no_credentials_raises():
    resolver = APIResolver()  # no credentials
    metadata = BookMetadata(title="New Book", authors=["New Author"])
    with pytest.raises(OLAuthRequired):
        resolver.create_edition(metadata)


def test_create_edition_success():
    resolver = APIResolver(ol_session_cookie="valid-session")
    with patch.object(resolver, "_find_or_create_author", return_value="/authors/OL123A"):
        with patch("httpx.Client") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"id": "/books/OL999M", "success": True}
            mock_resp.raise_for_status = MagicMock()
            mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            metadata = BookMetadata(title="New Book", authors=["New Author"])
            olid = resolver.create_edition(metadata)
    assert olid == 999


def test_create_edition_rate_limited_raises():
    resolver = APIResolver(ol_session_cookie="valid-session")
    with patch.object(resolver, "_find_or_create_author", return_value="/authors/OL123A"):
        with patch("httpx.Client") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "429", request=MagicMock(), response=mock_resp
            )
            mock_cls.return_value.__enter__.return_value.post.return_value = mock_resp
            with pytest.raises(OLRateLimited):
                resolver.create_edition(BookMetadata(title="Book", authors=["Author"]))


# --- _parse_olid ---

def test_parse_olid_from_full_path():
    assert APIResolver._parse_olid("/books/OL123M") == 123


def test_parse_olid_from_bare_key():
    assert APIResolver._parse_olid("OL456M") == 456


def test_parse_olid_author_key():
    assert APIResolver._parse_olid("/authors/OL789A") == 789


def test_parse_olid_empty_returns_none():
    assert APIResolver._parse_olid("") is None


def test_parse_olid_invalid_returns_none():
    assert APIResolver._parse_olid("/books/notanid") is None
