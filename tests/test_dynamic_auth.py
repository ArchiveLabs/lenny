
import os
import pytest
from unittest.mock import MagicMock, patch

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"

@pytest.fixture(scope="module")
def test_client():
    from fastapi.testclient import TestClient
    
    # Patch DB init and engine creation relative to where they are used/defined
    with patch("lenny.core.db.init"), \
         patch("lenny.core.db.create_engine"):
        
        # Import app inside patch context to ensure side-effects during import logic are mocked
        from lenny.app import app
        yield TestClient(app)

@pytest.mark.skip(reason="Requires updated pyopds2_lenny with build_catalog/empty_catalog methods")
@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_default_auth_mode_oauth(mock_get_items, test_client):
    """Test that default request without params results in OAuth mode links (no auth_mode param)."""
    mock_get_items.return_value = {}
    
    response = test_client.get("/v1/api/opds")
    assert response.status_code == 200
    data = response.json()
    
    # Check navigation for absence of auth_mode
    navs = data.get("navigation", [])
    assert navs, "Navigation should not be empty"
    for nav in navs:
        assert "auth_mode=direct" not in nav["href"]

@pytest.mark.skip(reason="Requires updated pyopds2_lenny with build_catalog/empty_catalog methods")
@patch("lenny.core.api.LennyDataProvider.search")
@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_dynamic_auth_param_propagation(mock_get_items, mock_search, test_client):
    # Mock items so we don't exit early (which would go to _build_empty_feed and skip links)
    mock_item = MagicMock()
    # Ensure it works with _build_query_and_lenny_ids expecting 'lenny' attr or valid OLID
    mock_item.lenny.openlibrary_edition = 123
    mock_get_items.return_value = {123: mock_item}
    
    # Mock search so we don't crash later
    mock_resp = MagicMock()
    mock_resp.records = []
    mock_resp.get_search_url.return_value = "/v1/api/opds/search"
    mock_resp.page = 1
    mock_resp.total = 0
    mock_resp.limit = 50
    mock_search.return_value = mock_resp
    
    response = test_client.get("/v1/api/opds?auth_mode=direct")
    assert response.status_code == 200
    data = response.json()
    
    # Check navigation - STICKY!
    navs = data.get("navigation", [])
    assert navs, "Navigation should not be empty"
    for nav in navs:
        assert "auth_mode=direct" in nav["href"]
        
    # Check "Bookshelf" link in links (if present)
    links = data.get("links", [])
    # Filter for relevant links to ensure we aren't asserting on empty list if filtering yields nothing by mistake
    target_rels = {"http://opds-spec.org/shelf", "profile"}
    sticky_links = [l for l in links if l.get("rel") in target_rels]
    
    assert sticky_links, "Should have found shelf/profile links"
    
    for link in sticky_links:
        assert "auth_mode=direct" in link["href"]

@pytest.mark.skip(reason="Requires updated pyopds2_lenny with build_catalog/empty_catalog methods")
@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_beta_flag_backward_compatibility(mock_get_items, test_client):
    """Test that ?beta=true still triggers direct mode logic."""
    mock_get_items.return_value = {}
    
    response = test_client.get("/v1/api/opds?beta=true")
    assert response.status_code == 200
    data = response.json()
    
    # Sticky navigation should be engaged
    navs = data.get("navigation", [])
    assert navs, "Navigation should not be empty"
    for nav in navs:
        assert "auth_mode=direct" in nav["href"]

@patch("lenny.routes.api.get_authenticated_email")
@patch("lenny.core.api.build_post_borrow_publication")
@patch("lenny.core.api.Loan.exists")
@patch("lenny.core.api.Item.exists")
@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_single_item_returns_post_borrow_when_authenticated_with_loan(
    mock_get_items, mock_item_exists, mock_loan_exists, mock_build_post_borrow, mock_get_email, test_client
):
    """Test that /opds/{book_id} returns post-borrow publication when user has an active loan."""
    # Setup mocks
    mock_item = MagicMock()
    mock_item.is_login_required = True
    mock_item.id = 1
    mock_item.subscription = False
    mock_item_exists.return_value = mock_item
    mock_loan_exists.return_value = True  # User has an active loan
    mock_get_email.return_value = "test@example.com"  # Mock authenticated user
    
    mock_build_post_borrow.return_value = {
        "@context": "https://readium.org/webpub-manifest/context.jsonld",
        "metadata": {"title": "Test Book"},
        "links": [
            {"rel": "http://opds-spec.org/acquisition/return", "href": "/v1/api/items/123/return"},
            {"rel": "http://opds-spec.org/acquisition/open-access", "href": "/v1/api/items/123/read", "title": "Read"}
        ]
    }
    
    response = test_client.get("/v1/api/opds/123")
    assert response.status_code == 200
    data = response.json()
    
    # Verify that build_post_borrow_publication was called (indicating loan was detected)
    mock_build_post_borrow.assert_called_once()
    call_args = mock_build_post_borrow.call_args
    assert call_args[0][0] == 123  # book_id
    
    # Verify the response contains return/read links
    links = data.get("links", [])
    rels = [link.get("rel") for link in links]
    assert "http://opds-spec.org/acquisition/return" in rels or any("return" in str(r) for r in rels)


@pytest.mark.skip(reason="Requires updated pyopds2_lenny with build_publication method")
@patch("lenny.core.api.Item.exists")
@patch("lenny.core.api.LennyAPI.get_enriched_items")
def test_single_item_no_loan_returns_standard_publication(
    mock_get_items, mock_item_exists, test_client
):
    """Test that /opds/{book_id} returns standard publication when user is not authenticated."""
    # Mock item exists but no loan (unauthenticated request)
    mock_item = MagicMock()
    mock_item.is_login_required = True
    mock_item.id = 1
    mock_item.lenny = MagicMock()
    mock_item.lenny.openlibrary_edition = 123
    mock_item.lenny.encrypted = False
    mock_item_exists.return_value = mock_item
    
    mock_get_items.return_value = {123: mock_item}
    
    with patch("lenny.core.api.LennyDataProvider.search") as mock_search:
        mock_record = MagicMock()
        mock_record.to_publication.return_value.model_dump.return_value = {
            "metadata": {"title": "Test Book"},
            "links": [{"rel": "http://opds-spec.org/acquisition/borrow", "href": "/v1/api/items/123/borrow"}]
        }
        mock_resp = MagicMock()
        mock_resp.records = [mock_record]
        mock_search.return_value = mock_resp
        
        response = test_client.get("/v1/api/opds/123")
        assert response.status_code == 200
        data = response.json()
        
        # Should have borrow link, not return link (unauthenticated)
        links = data.get("links", [])
        rels = [link.get("rel") for link in links]
        # Borrow link should be present for unauthenticated users
        assert any("borrow" in str(r) for r in rels) or "profile" in rels
