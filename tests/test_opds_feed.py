"""`LennyAPI.opds_feed` must reach Open Library exactly once per request.

lenny#194: the feed used to search Open Library twice with the same
`edition_key:(...)` disjunction — once to "enrich" the local items and once,
through `LennyDataProvider`, to build the publications. The first result set
was discarded in full: the only things that survived it were the edition ids
(which came from the local `Item` rows) and the encryption/availability flags
(which are also local). That doubled the response time of the whole feed.
"""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import unquote_plus

import pytest

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"


def _item(edition_id, encrypted=False, borrowable=True):
    item = MagicMock()
    item.openlibrary_edition = edition_id
    item.encrypted = encrypted
    item.is_borrowable = borrowable
    return item


def _ol_payload(edition_ids):
    """A minimal openlibrary.org/search.json body surfacing these editions."""
    return {
        "numFound": len(edition_ids),
        "docs": [
            {
                "key": f"/works/OL{edition_id}W",
                "title": f"Book {edition_id}",
                "editions": {
                    "numFound": 1,
                    "start": 0,
                    "numFoundExact": True,
                    "docs": [{
                        "key": f"/books/OL{edition_id}M",
                        "title": f"Book {edition_id}",
                    }],
                },
            }
            for edition_id in edition_ids
        ],
    }


@contextmanager
def count_ol_searches(payload):
    """Count outbound openlibrary.org/search.json requests.

    Both HTTP clients in the feed path are intercepted: `requests` (used by
    `pyopds2_openlibrary`) and `httpx` (used by `lenny.core.openlibrary`).
    Counting at the transport layer rather than at a mocked Python boundary is
    the point — it is the number this issue is about.
    """
    searches = []

    def _response():
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
            status_code=200,
        )

    def fake_requests_get(url, params=None, **_):
        if "search.json" in str(url):
            searches.append(("requests", str(url), params))
        return _response()

    def fake_httpx_get(_self, url, **_kwargs):
        if "search.json" in str(url):
            searches.append(("httpx", str(url), None))
        return _response()

    with patch("requests.get", fake_requests_get), \
         patch("httpx.Client.get", fake_httpx_get):
        yield searches


@contextmanager
def fake_open_library(order_for):
    """Serve `search.json`, letting the test pick the result order per query.

    Open Library ranks `{text} AND edition_key:(...)` by relevance to the text
    and a bare `edition_key:(...)` disjunction by something else entirely, so
    the two come back in different orders. `order_for(query)` reproduces that.
    """
    queries = []

    def _respond(query):
        queries.append(query)
        payload = _ol_payload(order_for(query))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
            status_code=200,
        )

    def fake_requests_get(url, params=None, **_):
        return _respond((params or {}).get("q", "") or unquote_plus(str(url)))

    def fake_httpx_get(_self, url, **_kwargs):
        # urlencode writes spaces as "+", so unquote alone would leave
        # "AND+edition_key:" and hide the very ordering split under test.
        return _respond(unquote_plus(str(url)))

    with patch("requests.get", fake_requests_get), \
         patch("httpx.Client.get", fake_httpx_get):
        yield queries


# ---------------------------------------------------------------------------
# The regression this module exists for
# ---------------------------------------------------------------------------

def test_opds_feed_issues_exactly_one_open_library_search():
    """One catalog request, one upstream search — not two."""
    from lenny.core.api import LennyAPI

    edition_ids = [37044497, 37044487, 51733522]
    items = [_item(i) for i in edition_ids]

    with patch("lenny.core.api.Item.get_many", return_value=items):
        with count_ol_searches(_ol_payload(edition_ids)) as searches:
            feed = LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    assert len(searches) == 1, (
        f"expected 1 outbound search.json request, got {len(searches)}: "
        f"{[url for _, url, _ in searches]}"
    )
    assert len(feed["publications"]) == len(edition_ids)


def test_opds_feed_does_not_call_the_enrichment_client():
    """`OpenLibrary.search` is no longer on the feed path at all."""
    from lenny.core.api import LennyAPI

    edition_ids = [10, 20]
    items = [_item(i) for i in edition_ids]

    with patch("lenny.core.api.Item.get_many", return_value=items), \
         patch("lenny.core.api.OpenLibrary.search") as mock_enrich, \
         patch("lenny.core.api.LennyDataProvider.search") as mock_provider, \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={"ok": True}):
        mock_provider.return_value = SimpleNamespace(records=[MagicMock()])
        LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    mock_enrich.assert_not_called()
    mock_provider.assert_called_once()


# ---------------------------------------------------------------------------
# What the discarded search used to supply, now read locally
# ---------------------------------------------------------------------------

def test_opds_feed_takes_edition_ids_and_flags_from_local_rows():
    from lenny.core.api import LennyAPI

    items = [
        _item(10, encrypted=False, borrowable=True),
        _item(20, encrypted=True, borrowable=False),
    ]

    with patch("lenny.core.api.Item.get_many", return_value=items), \
         patch("lenny.core.api.LennyDataProvider.search") as mock_provider, \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={"ok": True}):
        mock_provider.return_value = SimpleNamespace(records=[MagicMock()])
        LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    kwargs = mock_provider.call_args.kwargs
    assert kwargs["query"] == "edition_key:(OL10M OR OL20M)"
    assert kwargs["lenny_ids"] == {10: 10, 20: 20}
    assert kwargs["encryption_map"] == {10: False, 20: True}
    assert kwargs["borrowable_map"] == {10: True, 20: False}


def test_opds_feed_publications_keep_their_own_identity():
    """End to end: each publication's links address its own edition.

    Open Library does not return an `edition_key:` disjunction in the order it
    was asked for, so this is the property the whole two-PR change rests on.
    """
    from lenny.core.api import LennyAPI

    edition_ids = [37044497, 37044487, 51733522]
    items = [_item(i, encrypted=(i == 37044487)) for i in edition_ids]

    # Open Library answers in a different order than it was asked.
    payload = _ol_payload(list(reversed(edition_ids)))

    with patch("lenny.core.api.Item.get_many", return_value=items):
        with count_ol_searches(payload):
            feed = LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    for publication in feed["publications"]:
        title = publication["metadata"]["title"]
        edition_id = int(title.removeprefix("Book "))
        for link in publication["links"]:
            if "/items/" in link["href"] or "/opds/" in link["href"]:
                assert f"/{edition_id}" in link["href"], (
                    f"{title} links to {link['href']}"
                )


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def test_opds_feed_paginates_locally_and_asks_open_library_for_page_one():
    """`offset` selects local rows; the upstream query must not re-paginate.

    The disjunction sent upstream contains only this page's edition ids, so
    asking Open Library for page `offset // limit + 1` of it returned nothing.
    """
    from lenny.core.api import LennyAPI

    items = [_item(i) for i in (30, 40)]

    with patch("lenny.core.api.Item.get_many", return_value=items) as mock_get_many, \
         patch("lenny.core.api.LennyDataProvider.search") as mock_provider, \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={"ok": True}):
        mock_provider.return_value = SimpleNamespace(records=[MagicMock()])
        LennyAPI.opds_feed(offset=50, limit=50, auth_mode_direct=False)

    assert mock_get_many.call_args.kwargs == {"offset": 50, "limit": 50}
    assert mock_provider.call_args.kwargs["offset"] == 0


def test_opds_feed_second_page_returns_its_publications():
    """Regression: page 2 of a paged catalog used to come back empty."""
    from lenny.core.api import LennyAPI

    page_two_ids = [30, 40]
    items = [_item(i) for i in page_two_ids]

    with patch("lenny.core.api.Item.get_many", return_value=items):
        with count_ol_searches(_ol_payload(page_two_ids)) as searches:
            feed = LennyAPI.opds_feed(offset=50, limit=50, auth_mode_direct=False)

    assert len(searches) == 1
    assert "page=1" in searches[0][1] or "page" not in searches[0][1]
    assert len(feed["publications"]) == 2


# ---------------------------------------------------------------------------
# Empty and degraded paths
# ---------------------------------------------------------------------------

def test_opds_feed_no_local_items_returns_empty_catalog_without_touching_ol():
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.get_many", return_value=[]):
        with count_ol_searches(_ol_payload([])) as searches:
            feed = LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    assert searches == []
    assert feed["publications"] == []


def test_opds_feed_when_open_library_knows_none_of_the_editions():
    """Filtering used to happen in the discarded search; it still happens."""
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.get_many", return_value=[_item(10)]):
        with count_ol_searches(_ol_payload([])) as searches:
            feed = LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    assert len(searches) == 1
    assert feed["publications"] == []


def test_opds_feed_single_item_unknown_to_open_library_does_not_crash():
    """The `olid` path indexes `records[0]`; an empty result must not raise."""
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.exists", return_value=_item(10)):
        with count_ol_searches(_ol_payload([])):
            feed = LennyAPI.opds_feed(olid=10, auth_mode_direct=False)

    assert feed["publications"] == []


def test_opds_feed_returns_empty_catalog_when_open_library_is_unreachable():
    from lenny.core.api import LennyAPI
    import httpx

    def boom(*_, **__):
        raise httpx.ConnectError("nope")

    with patch("lenny.core.api.Item.get_many", return_value=[_item(10)]), \
         patch("lenny.core.api.LennyDataProvider.search", side_effect=boom):
        feed = LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    assert feed["publications"] == []


# ---------------------------------------------------------------------------
# /v1/api/opds/search — the endpoint that was mis-assigning links in production
# ---------------------------------------------------------------------------
#
# The plain /opds feed got away with positional keying because both of its
# searches sent the *identical* `edition_key:(...)` disjunction, so Open Library
# returned the same order twice (measured live: 43/43 positions agree).
#
# search_feed had no such luck. Its first search was
# `{query} AND edition_key:(...)`, ranked by relevance to the text; its second
# was a bare disjunction over the matched subset, ranked by something else.
# Measured live against openlibrary.org with the production catalog:
#
#     query='the'   6/24 positions agree   ->  18 publications mis-linked
#     query='a'     1/41 positions agree   ->  40 publications mis-linked
#     query='of'    0/14 positions agree   ->  14 publications mis-linked
#
# Confirmed against the deployed instance: 'The Awakening' was served carrying
# lenny_id OL37044610M, which is 'The Prince and the Pauper'.

# Relevance order differs from disjunction order, as it does upstream.
SEARCH_IDS = [37044696, 37044525, 51733522, 37044731]
RELEVANCE_ORDER = [51733522, 37044696, 37044731, 37044525]


def _order_by_query(query):
    """Relevance order for the text search, catalog order for a bare disjunction."""
    return RELEVANCE_ORDER if "AND edition_key:" in query else SEARCH_IDS


def test_search_feed_publications_keep_their_own_identity():
    """The production regression: every search hit keeps its own lenny_id.

    On the two-search implementation this fails outright — the publications are
    ordered by relevance but the ids are assigned from a differently-ordered
    second query, so each one is handed another book's borrow link.
    """
    from lenny.core.api import LennyAPI

    all_items = {i: _item(i) for i in SEARCH_IDS}

    with patch("lenny.core.api.Item.get_all", return_value=all_items):
        with fake_open_library(_order_by_query):
            feed = LennyAPI.search_feed(query="of", limit=50, auth_mode_direct=False)

    publications = feed["publications"]
    assert len(publications) == len(SEARCH_IDS)

    for publication in publications:
        title = publication["metadata"]["title"]
        edition_id = int(title.removeprefix("Book "))
        for link in publication["links"]:
            if "/items/" in link["href"] or "/opds/" in link["href"]:
                assert f"/{edition_id}" in link["href"], (
                    f"{title} was given {link['href']}"
                )


def test_search_feed_issues_exactly_one_open_library_search_per_batch():
    from lenny.core.api import LennyAPI

    all_items = {i: _item(i) for i in SEARCH_IDS}

    with patch("lenny.core.api.Item.get_all", return_value=all_items):
        with count_ol_searches(_ol_payload(SEARCH_IDS)) as searches:
            LennyAPI.search_feed(query="of", limit=50, auth_mode_direct=False)

    assert len(searches) == 1, (
        f"expected 1 outbound search.json request, got {len(searches)}"
    )


def test_search_feed_scopes_its_single_search_to_local_editions():
    """The one remaining search still carries both the text and the id scope."""
    from lenny.core.api import LennyAPI

    all_items = {i: _item(i) for i in SEARCH_IDS}

    with patch("lenny.core.api.Item.get_all", return_value=all_items):
        with fake_open_library(_order_by_query) as queries:
            LennyAPI.search_feed(query="of", limit=50, auth_mode_direct=False)

    assert len(queries) == 1
    assert queries[0].startswith("of AND edition_key:(")
    for edition_id in SEARCH_IDS:
        assert f"OL{edition_id}M" in queries[0]


def test_search_feed_drops_hits_lenny_does_not_hold():
    """A record with no lenny_id is not one of Lenny's and must not be served."""
    from lenny.core.api import LennyAPI

    held = SEARCH_IDS[:2]
    all_items = {i: _item(i) for i in held}

    # Open Library answers with two extra editions Lenny does not have.
    with patch("lenny.core.api.Item.get_all", return_value=all_items):
        with fake_open_library(lambda _q: RELEVANCE_ORDER):
            feed = LennyAPI.search_feed(query="of", limit=50, auth_mode_direct=False)

    served = {int(p["metadata"]["title"].removeprefix("Book "))
              for p in feed["publications"]}
    assert served == set(held)


def test_search_feed_deduplicates_across_batches():
    """Batches overlap only via OL; the same edition must not appear twice."""
    from lenny.core.api import LennyAPI

    all_items = {i: _item(i) for i in SEARCH_IDS}

    with patch("lenny.core.api.Item.get_all", return_value=all_items), \
         patch("lenny.core.api.LennyAPI.SEARCH_BATCH_SIZE", 2):
        with fake_open_library(lambda _q: SEARCH_IDS) as queries:
            feed = LennyAPI.search_feed(query="of", limit=50, auth_mode_direct=False)

    assert len(queries) == 2, "two batches of two"
    served = [int(p["metadata"]["title"].removeprefix("Book "))
              for p in feed["publications"]]
    assert len(served) == len(set(served)), f"duplicate publications: {served}"


def test_opds_feed_skips_rows_with_an_unusable_edition_id():
    from lenny.core.api import LennyAPI

    good = _item(10)
    bad = MagicMock()
    bad.openlibrary_edition = None

    with patch("lenny.core.api.Item.get_many", return_value=[good, bad, None]), \
         patch("lenny.core.api.LennyDataProvider.search") as mock_provider, \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={"ok": True}):
        mock_provider.return_value = SimpleNamespace(records=[MagicMock()])
        LennyAPI.opds_feed(limit=50, auth_mode_direct=False)

    assert mock_provider.call_args.kwargs["query"] == "edition_key:(OL10M)"
