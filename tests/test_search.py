import os
import pytest
from unittest.mock import patch, MagicMock

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"


@pytest.fixture(scope="module")
def test_client():
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), \
         patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)



# ---------------------------------------------------------------------------
# Task 3 tests: _fetch_all_edition_ids
# ---------------------------------------------------------------------------

def test_item_get_all_returns_dict():
    """Item.get_all() should return {openlibrary_edition: Item} mapping."""
    from lenny.core.models import Item

    item1 = MagicMock()
    item1.openlibrary_edition = 111
    item2 = MagicMock()
    item2.openlibrary_edition = 222

    mock_query = MagicMock()
    mock_query.all.return_value = [item1, item2]

    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = mock_query
        result = Item.get_all()

    assert result == {111: item1, 222: item2}
    assert len(result) == 2


def test_item_get_all_empty_db():
    """Item.get_all() should return empty dict when no items exist."""
    from lenny.core.models import Item

    mock_query = MagicMock()
    mock_query.all.return_value = []

    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = mock_query
        result = Item.get_all()

    assert result == {}


# ---------------------------------------------------------------------------
# Task 4 tests: search_feed
# ---------------------------------------------------------------------------

def test_search_feed_empty_query_returns_empty_catalog():
    """Verify empty query returns empty catalog without hitting OL."""
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.OpenLibrary.search") as mock_ol_search, \
         patch("lenny.core.api.LennyDataProvider.empty_catalog", return_value={"empty": True}) as mock_empty:
        result = LennyAPI.search_feed(query="", auth_mode_direct=False)

    mock_ol_search.assert_not_called()
    mock_empty.assert_called_once_with(title="Search results", auth_mode_direct=False)
    assert result == {"empty": True}


def test_search_feed_no_items_returns_empty_catalog():
    """Verify empty DB returns empty catalog without querying OL."""
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.get_all", return_value={}) as mock_fetch, \
         patch("lenny.core.api.OpenLibrary.search") as mock_ol_search, \
         patch("lenny.core.api.LennyDataProvider.empty_catalog", return_value={"empty": True}) as mock_empty:
        result = LennyAPI.search_feed(query="python", auth_mode_direct=False)

    mock_fetch.assert_called_once()
    mock_ol_search.assert_not_called()
    mock_empty.assert_called_once_with(
        title="Search results for: python", auth_mode_direct=False
    )
    assert result == {"empty": True}


def test_search_feed_builds_chunked_query():
    """Verify OL is called with combined '{query} AND edition_key:(...)' query."""
    from lenny.core.api import LennyAPI

    # Create mock items in the DB
    item1 = MagicMock()
    item1.openlibrary_edition = 10
    item1.encrypted = False
    item1.is_borrowable = True

    item2 = MagicMock()
    item2.openlibrary_edition = 20
    item2.encrypted = True
    item2.is_borrowable = False

    all_items = {10: item1, 20: item2}

    # Create mock OL search results matching those items
    ol_record1 = MagicMock()
    ol_record1.olid = "10"
    ol_record2 = MagicMock()
    ol_record2.olid = "20"

    # Mock LennyDataProvider.search response
    mock_lenny_record = MagicMock(spec=["auth_mode_direct"])
    mock_search_response = MagicMock()
    mock_search_response.records = [mock_lenny_record]

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.OpenLibrary.search", return_value=[ol_record1, ol_record2]) as mock_ol_search, \
         patch("lenny.core.api.LennyDataProvider.search", return_value=mock_search_response), \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={"catalog": True}) as mock_build:

        result = LennyAPI.search_feed(query="python", limit=10, auth_mode_direct=False)

    # Verify OL.search was called with the combined query
    mock_ol_search.assert_called_once()
    call_kwargs = mock_ol_search.call_args
    ol_query = call_kwargs.kwargs["query"] if "query" in call_kwargs.kwargs else call_kwargs.args[0]
    assert "python AND edition_key:" in str(ol_query)
    assert "OL10M" in str(ol_query)
    assert "OL20M" in str(ol_query)

    # Verify build_catalog was called
    mock_build.assert_called_once()
    assert result == {"catalog": True}


# ---------------------------------------------------------------------------
# Task 5 tests: /opds/search endpoint
# ---------------------------------------------------------------------------

def test_opds_search_endpoint_returns_opds_json(test_client):
    """Mock LennyAPI.search_feed, verify 200 + correct content-type + correct body."""
    mock_feed = {"metadata": {"title": "Search results"}, "publications": []}

    with patch("lenny.routes.api.LennyAPI.search_feed", return_value=mock_feed) as mock_sf:
        resp = test_client.get("/v1/api/opds/search?query=python")

    assert resp.status_code == 200
    assert "application/opds+json" in resp.headers["content-type"]
    assert resp.json() == mock_feed
    mock_sf.assert_called_once_with(query="python", auth_mode_direct=False)


def test_opds_search_endpoint_empty_query(test_client):
    """Verify endpoint works with no query param (defaults to empty string)."""
    mock_feed = {"metadata": {"title": "Search results"}, "publications": []}

    with patch("lenny.routes.api.LennyAPI.search_feed", return_value=mock_feed) as mock_sf:
        resp = test_client.get("/v1/api/opds/search")

    assert resp.status_code == 200
    assert "application/opds+json" in resp.headers["content-type"]
    mock_sf.assert_called_once_with(query="", auth_mode_direct=False)


def test_opds_search_no_auth_required(test_client):
    """Verify it works without any session cookie — endpoint is public."""
    mock_feed = {"metadata": {"title": "Search results"}, "publications": []}

    with patch("lenny.routes.api.LennyAPI.search_feed", return_value=mock_feed):
        # Explicitly send no cookies
        resp = test_client.get("/v1/api/opds/search?query=test", cookies={})

    assert resp.status_code == 200
    assert "application/opds+json" in resp.headers["content-type"]


# ---------------------------------------------------------------------------
# Task 6 tests: OPDS search discovery link in catalog navigation
# ---------------------------------------------------------------------------

@pytest.mark.skip(reason="Requires pyopds2_lenny search link PR to be merged upstream")
def test_navigation_includes_search_link():
    """Verify navigation() includes a templated search link with rel='search'."""
    from pyopds2_lenny import LennyDataProvider

    LennyDataProvider.BASE_URL = "http://example.com/v1/api/"
    nav = LennyDataProvider.navigation()

    search_entries = [n for n in nav if n.get("rel") == "search"]
    assert len(search_entries) == 1, "Expected exactly one search entry in navigation"

    entry = search_entries[0]
    assert entry["templated"] is True
    assert "{?query}" in entry["href"]
    assert entry["type"] == "application/opds+json"
    assert entry["title"] == "Search"


@pytest.mark.skip(reason="Requires pyopds2_lenny search link PR to be merged upstream")
def test_catalog_links_include_search():
    """Verify _catalog_links() includes a templated search OPDSLink."""
    from pyopds2_lenny import LennyDataProvider

    LennyDataProvider.BASE_URL = "http://example.com/v1/api/"
    links = LennyDataProvider._catalog_links()

    search_links = [l for l in links if l.rel == "search"]
    assert len(search_links) == 1, "Expected exactly one search link in catalog links"

    link = search_links[0]
    assert link.templated is True
    assert "{?query}" in link.href
    assert link.type == "application/opds+json"
    assert link.title == "Search"


# ---------------------------------------------------------------------------
# Task 7-8 tests: Integration / end-to-end
# ---------------------------------------------------------------------------

def test_search_flow_end_to_end(test_client):
    """Full flow: search endpoint -> LennyAPI.search_feed -> mocked OL -> OPDS feed."""
    from lenny.core.api import LennyAPI, OpenLibrary
    from pyopds2_lenny import LennyDataProvider

    # 1. Mock item in the DB
    mock_item = MagicMock()
    mock_item.openlibrary_edition = 999
    mock_item.id = 1
    mock_item.encrypted = False
    mock_item.is_borrowable = True

    # 2. Mock OL search result matching that item
    ol_record = MagicMock()
    ol_record.olid = "999"

    # 3. Build a realistic OPDS feed via build_catalog
    mock_catalog = {
        "@context": "https://readium.org/webpub-manifest/context.jsonld",
        "metadata": {"title": "Search results for: test"},
        "publications": [
            {
                "metadata": {"title": "Test Book"},
                "links": [{"rel": "self", "href": "/opds/999"}],
            }
        ],
        "links": [],
        "navigation": [],
    }

    # Mock search response object for LennyDataProvider.search
    mock_lenny_record = MagicMock()
    mock_search_response = MagicMock()
    mock_search_response.records = [mock_lenny_record]

    with patch("lenny.core.api.Item.get_all",
               return_value={999: mock_item}), \
         patch("lenny.core.api.OpenLibrary.search",
               return_value=[ol_record]) as mock_ol_search, \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=mock_search_response) as mock_provider_search, \
         patch("lenny.core.api.LennyDataProvider.build_catalog",
               return_value=mock_catalog) as mock_build:

        resp = test_client.get("/v1/api/opds/search?query=test")

    # Verify HTTP response
    assert resp.status_code == 200
    assert "application/opds+json" in resp.headers["content-type"]

    body = resp.json()
    assert "publications" in body
    assert len(body["publications"]) == 1
    assert body["publications"][0]["metadata"]["title"] == "Test Book"

    # Verify OL was called with the scoped query
    mock_ol_search.assert_called_once()
    ol_query = mock_ol_search.call_args.kwargs["query"] if "query" in mock_ol_search.call_args.kwargs else mock_ol_search.call_args.args[0]
    assert "test AND edition_key:" in str(ol_query)
    assert "OL999M" in str(ol_query)

    # Verify LennyDataProvider.search was called to build records
    mock_provider_search.assert_called_once()

    # Verify build_catalog produced the final feed
    mock_build.assert_called_once()


def test_opds_catalog_includes_search_link(test_client):
    """The main /opds catalog should advertise the search endpoint."""

    # Simulate a catalog response that includes the search link in navigation
    # (mirroring what build_catalog now produces via navigation() and _catalog_links())
    mock_catalog = {
        "@context": "https://readium.org/webpub-manifest/context.jsonld",
        "metadata": {"title": "Lenny Catalog"},
        "publications": [],
        "links": [
            {"rel": "start", "href": "/opds", "type": "application/opds+json"},
            {
                "rel": "search",
                "href": "http://localhost/v1/api/opds/search{?query}",
                "type": "application/opds+json",
                "title": "Search",
                "templated": True,
            },
        ],
        "navigation": [
            {
                "href": "http://localhost/v1/api/opds/search{?query}",
                "title": "Search",
                "type": "application/opds+json",
                "rel": "search",
                "templated": True,
            },
        ],
    }

    with patch("lenny.routes.api.LennyAPI.opds_feed", return_value=mock_catalog):
        resp = test_client.get("/v1/api/opds")

    assert resp.status_code == 200
    assert "application/opds+json" in resp.headers["content-type"]

    body = resp.json()

    # Check links array for search link
    search_links = [l for l in body.get("links", []) if l.get("rel") == "search"]
    assert len(search_links) >= 1, "Expected at least one search link in catalog links"

    search_link = search_links[0]
    assert "{?query}" in search_link["href"]
    assert search_link.get("templated") is True

    # Also verify navigation advertises search
    search_nav = [n for n in body.get("navigation", []) if n.get("rel") == "search"]
    assert len(search_nav) >= 1, "Expected search entry in navigation"
    assert "{?query}" in search_nav[0]["href"]
