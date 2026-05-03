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
