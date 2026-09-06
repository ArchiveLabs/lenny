"""OPDS incremental-harvest support: `modified_since`, `metadata.modified`, `rel=next`.

Open Library's BookWorm harvester (internetarchive/openlibrary#13241,
`openlibrary/bookworm/harvest.py`) needs three things from a feed to harvest it
incrementally rather than refetching everything each tick:

  1. a `modified_since=<YYYY-MM-DD>` query param it can inject into the feed URL,
  2. a `rel=next` link to follow, so it sees past the first page,
  3. `metadata.modified` on each publication, to advance a client-side cursor.

These tests pin all three, plus the two traps that make them easy to get wrong:
the feed must stay `json.dumps`-able, and paging must not be double-applied.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"


# ---------------------------------------------------------------------------
# parse_modified_since / to_iso_utc
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        # The exact shape OL sends: `since.date().isoformat()`.
        ("2026-08-01", datetime(2026, 8, 1, tzinfo=timezone.utc)),
        ("2026-08-01T12:30:00Z", datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)),
        ("2026-08-01T12:30:00z", datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)),
        ("2026-08-01T12:30:00+00:00", datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)),
        # An explicit offset is normalized to UTC, not merely accepted.
        ("2026-08-01T12:30:00-05:00", datetime(2026, 8, 1, 17, 30, tzinfo=timezone.utc)),
        # Naive means UTC.
        ("2026-08-01T12:30:00", datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)),
    ],
)
def test_parse_modified_since_accepts_iso_forms(raw, expected):
    from lenny.core.utils import parse_modified_since

    assert parse_modified_since(raw) == expected


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_parse_modified_since_treats_blank_as_no_filter(blank):
    from lenny.core.utils import parse_modified_since

    assert parse_modified_since(blank) is None


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "08/01/2026", "1754006400"])
def test_parse_modified_since_rejects_junk(bad):
    """Must raise, not return None. Returning None would silently serve the whole
    catalogue as though no filter had been requested."""
    from lenny.core.utils import parse_modified_since

    with pytest.raises(ValueError):
        parse_modified_since(bad)


def test_parse_modified_since_passes_through_datetime():
    from lenny.core.utils import parse_modified_since

    naive = datetime(2026, 8, 1, 9, 0)
    assert parse_modified_since(naive) == datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)

    aware = datetime(2026, 8, 1, 9, 0, tzinfo=timezone(timedelta(hours=2)))
    assert parse_modified_since(aware) == datetime(2026, 8, 1, 7, 0, tzinfo=timezone.utc)


def test_to_iso_utc_renders_z_suffix():
    from lenny.core.utils import to_iso_utc

    assert to_iso_utc(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)) == "2026-08-01T12:00:00Z"
    # Naive is read as UTC, matching parse_modified_since.
    assert to_iso_utc(datetime(2026, 8, 1, 12, 0)) == "2026-08-01T12:00:00Z"
    # Non-UTC is converted, not just labelled.
    assert to_iso_utc(
        datetime(2026, 8, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    ) == "2026-08-01T17:00:00Z"
    assert to_iso_utc(None) is None


def test_iso_roundtrip_is_stable():
    """A cursor advanced from a feed's own `modified` value must reparse to the
    same instant, or a harvester drifts every cycle."""
    from lenny.core.utils import parse_modified_since, to_iso_utc

    original = datetime(2026, 8, 1, 12, 34, 56, tzinfo=timezone.utc)
    assert parse_modified_since(to_iso_utc(original)) == original


# ---------------------------------------------------------------------------
# Item.get_many / Item.count
# ---------------------------------------------------------------------------

def _query_chain():
    """A MagicMock that mimics SQLAlchemy's fluent Query, recording each call."""
    q = MagicMock()
    for method in ("filter", "order_by", "offset", "limit"):
        getattr(q, method).return_value = q
    q.all.return_value = []
    q.count.return_value = 0
    return q


def test_get_many_filters_on_modified_since():
    from lenny.core.models import Item

    q = _query_chain()
    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = q
        Item.get_many(modified_since=since)

    assert q.filter.call_count == 1, "expected exactly the modified_since filter"
    # The filter is a SQLAlchemy BinaryExpression; compiling it is the only way
    # to confirm we filtered the right column with the right operator.
    clause = str(q.filter.call_args.args[0])
    assert "updated_at" in clause and ">=" in clause


def test_get_many_without_modified_since_adds_no_filter():
    from lenny.core.models import Item

    q = _query_chain()
    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = q
        Item.get_many(offset=10, limit=5)

    q.filter.assert_not_called()
    q.offset.assert_called_once_with(10)
    q.limit.assert_called_once_with(5)


def test_get_many_orders_by_updated_at_then_id():
    """Ordering is what makes offset/limit paging stable and makes
    `modified_since` harvesting resumable. Without it Postgres may return rows in
    any order and pages can drop or duplicate items."""
    from lenny.core.models import Item

    q = _query_chain()
    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = q
        Item.get_many()

    q.order_by.assert_called_once()
    ordering = [str(c) for c in q.order_by.call_args.args]
    assert len(ordering) == 2
    assert "updated_at" in ordering[0] and "ASC" in ordering[0].upper()
    assert "id" in ordering[1]


def test_get_many_combines_encrypted_and_modified_since():
    from lenny.core.models import Item

    q = _query_chain()
    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = q
        Item.get_many(encrypted=True, modified_since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert q.filter.call_count == 2
    clauses = " ".join(str(c.args[0]) for c in q.filter.call_args_list)
    assert "encrypted" in clauses and "updated_at" in clauses


def test_count_applies_same_filters_but_no_paging():
    from lenny.core.models import Item

    q = _query_chain()
    q.count.return_value = 42
    with patch("lenny.core.models.db") as mock_db:
        mock_db.query.return_value = q
        total = Item.count(modified_since=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert total == 42
    assert q.filter.call_count == 1
    q.offset.assert_not_called()
    q.limit.assert_not_called()


# ---------------------------------------------------------------------------
# Catalog links: self + rel=next
# ---------------------------------------------------------------------------

def _links(**page):
    from lenny.core.api import _lenny_catalog_links

    return {
        link.rel: link.href
        for link in _lenny_catalog_links("https://lenny.test/v1/api/", page=page or None)
    }


def test_no_next_link_on_the_last_page():
    assert "next" not in _links(offset=40, limit=10, total=50)


def test_next_link_present_when_more_items_remain():
    nxt = _links(offset=0, limit=10, total=50)["next"]
    assert "offset=10" in nxt and "limit=10" in nxt


def test_next_link_carries_the_modified_since_filter():
    """Dropping the filter on page two would silently walk the consumer off the
    filtered set and back onto the full catalogue."""
    nxt = _links(offset=0, limit=10, total=50, modified_since="2026-08-01T00:00:00Z")["next"]
    assert "modified_since=2026-08-01T00%3A00%3A00Z" in nxt
    assert "offset=10" in nxt


def test_self_link_reflects_the_request():
    links = _links(offset=10, limit=10, total=50, modified_since="2026-08-01T00:00:00Z")
    assert "offset=10" in links["self"]
    assert "modified_since=2026-08-01T00%3A00%3A00Z" in links["self"]


def test_self_link_stays_bare_without_paging_context():
    """Unparameterized requests keep the URL clients have always seen."""
    links = _links()
    assert links["self"] == "https://lenny.test/v1/api/opds"
    assert "next" not in links


def test_no_next_link_when_total_is_unknown():
    assert "next" not in _links(offset=0, limit=10)


def test_search_and_shelf_links_are_preserved():
    """Regression guard: the paging rework must not drop the existing links."""
    links = _links(offset=0, limit=10, total=50)
    assert links["search"] == "https://lenny.test/v1/api/opds/search{?query}"
    assert "http://opds-spec.org/shelf" in links
    assert "profile" in links


# ---------------------------------------------------------------------------
# metadata.modified on publications
# ---------------------------------------------------------------------------

def _fake_record(lenny_id, title):
    """A stand-in for LennyDataRecord that serializes like the real thing."""
    from pyopds2.models import Link, Metadata, Publication

    record = MagicMock(spec=["lenny_id", "to_publication", "links"])
    record.lenny_id = lenny_id
    record.to_publication.return_value = Publication(metadata=Metadata(title=title))
    record.links.return_value = [
        Link(rel="self", href=f"https://lenny.test/v1/api/opds/{lenny_id}",
             type="application/opds-publication+json")
    ]
    return record


def _search_response(*records):
    resp = MagicMock()
    resp.records = list(records)
    return resp


def test_build_catalog_stamps_modified_per_publication():
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(
        _search_response(_fake_record(1, "One"), _fake_record(2, "Two")),
        modified_map={1: "2026-08-01T00:00:00Z", 2: "2026-08-02T00:00:00Z"},
    )

    stamps = [p["metadata"]["modified"] for p in feed["publications"]]
    assert stamps == ["2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z"]


def test_build_catalog_keys_modified_by_lenny_id_not_position():
    """The map is keyed by lenny_id, so a record's timestamp follows the item even
    when the Open Library search response comes back in a different order."""
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(
        _search_response(_fake_record(2, "Two"), _fake_record(1, "One")),
        modified_map={1: "2026-08-01T00:00:00Z", 2: "2026-08-02T00:00:00Z"},
    )

    by_title = {p["metadata"]["title"]: p["metadata"]["modified"] for p in feed["publications"]}
    assert by_title == {"Two": "2026-08-02T00:00:00Z", "One": "2026-08-01T00:00:00Z"}


def test_build_catalog_omits_modified_when_unknown():
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(
        _search_response(_fake_record(1, "One")), modified_map={99: "2026-08-01T00:00:00Z"}
    )
    assert "modified" not in feed["publications"][0]["metadata"]


def test_build_catalog_without_modified_map_is_unchanged():
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(_search_response(_fake_record(1, "One")))
    assert "modified" not in feed["publications"][0]["metadata"]


def test_feed_with_modified_stays_json_serializable():
    """The trap this guards: `Catalog.publications` is typed `List[Publication]`,
    so a `modified` written before `model_dump()` gets re-validated by pydantic
    into a `datetime` — and the route hands the feed straight to `json.dumps`,
    which raises on datetimes and turns the whole feed into a 503."""
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(
        _search_response(_fake_record(1, "One")),
        modified_map={1: "2026-08-01T00:00:00Z"},
        page={"offset": 0, "limit": 1, "total": 5, "modified_since": "2026-08-01T00:00:00Z"},
    )

    reparsed = json.loads(json.dumps(feed))
    assert reparsed["publications"][0]["metadata"]["modified"] == "2026-08-01T00:00:00Z"


def test_build_publication_stamps_modified():
    from lenny.core.api import LennyDataProvider

    pub = LennyDataProvider.build_publication(
        _fake_record(7, "Seven"), modified_map={7: "2026-08-03T00:00:00Z"}
    )
    assert pub["metadata"]["modified"] == "2026-08-03T00:00:00Z"
    json.dumps(pub)  # must not raise


def test_build_catalog_reports_total_not_page_size():
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(
        _search_response(_fake_record(1, "One")),
        page={"offset": 0, "limit": 1, "total": 137},
    )
    assert feed["metadata"]["numberOfItems"] == 137


def test_build_catalog_falls_back_to_page_size_without_total():
    """search_feed builds catalogs with no paging context; it must keep working."""
    from lenny.core.api import LennyDataProvider

    feed = LennyDataProvider.build_catalog(
        _search_response(_fake_record(1, "One"), _fake_record(2, "Two"))
    )
    assert feed["metadata"]["numberOfItems"] == 2


# ---------------------------------------------------------------------------
# opds_feed wiring
# ---------------------------------------------------------------------------

def _item(edition, updated_at, encrypted=False):
    item = MagicMock()
    item.openlibrary_edition = edition
    item.encrypted = encrypted
    item.is_borrowable = True
    item.updated_at = updated_at
    return item


def test_opds_feed_passes_modified_since_to_the_db_query():
    from lenny.core.api import LennyAPI

    since = datetime(2026, 8, 1, tzinfo=timezone.utc)
    with patch("lenny.core.api.Item.get_many", return_value=[]) as get_many, \
         patch("lenny.core.api.Item.count", return_value=0), \
         patch("lenny.core.api.LennyDataProvider.empty_catalog", return_value={}):
        LennyAPI.opds_feed(modified_since=since, limit=10)

    assert get_many.call_args.kwargs["modified_since"] == since


def test_opds_feed_accepts_a_string_modified_since():
    """The route hands over a datetime, but the classmethod is called directly by
    scripts and tests too, so a bare ISO date must work."""
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.get_many", return_value=[]) as get_many, \
         patch("lenny.core.api.Item.count", return_value=0), \
         patch("lenny.core.api.LennyDataProvider.empty_catalog", return_value={}):
        LennyAPI.opds_feed(modified_since="2026-08-01", limit=10)

    assert get_many.call_args.kwargs["modified_since"] == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_opds_feed_does_not_reapply_offset_to_the_ol_search():
    """Regression guard. `get_many` already applied the offset, and the OL query
    names only this page's editions — offsetting again skips the entire narrowed
    result set. That is why /opds?offset=50 served an empty feed, which would
    have made `rel=next` useless."""
    from lenny.core.api import LennyAPI

    items = [_item(10, datetime(2026, 8, 1, tzinfo=timezone.utc))]
    with patch("lenny.core.api.Item.get_many", return_value=[_item(10, None)]), \
         patch("lenny.core.api.Item.count", return_value=100), \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=_search_response(_fake_record(10, "Ten"))) as search, \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={}):
        LennyAPI.opds_feed(offset=50, limit=10)

    assert search.call_args.kwargs.get("offset", 0) == 0


def test_opds_feed_builds_modified_map_from_item_updated_at():
    from lenny.core.api import LennyAPI

    items = [
        _item(10, datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)),
        _item(20, datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)),
    ]
    with patch("lenny.core.api.Item.get_many", return_value=items), \
         patch("lenny.core.api.Item.count", return_value=2), \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=_search_response(_fake_record(10, "Ten"), _fake_record(20, "Twenty"))), \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={}) as build:
        LennyAPI.opds_feed(limit=10)

    assert build.call_args.kwargs["modified_map"] == {
        10: "2026-08-01T06:00:00Z",
        20: "2026-08-02T06:00:00Z",
    }


def test_opds_feed_tolerates_items_with_no_updated_at():
    from lenny.core.api import LennyAPI

    items = [_item(10, None), _item(20, datetime(2026, 8, 2, tzinfo=timezone.utc))]
    with patch("lenny.core.api.Item.get_many", return_value=items), \
         patch("lenny.core.api.Item.count", return_value=2), \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=_search_response(_fake_record(10, "Ten"), _fake_record(20, "Twenty"))), \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={}) as build:
        LennyAPI.opds_feed(limit=10)

    assert build.call_args.kwargs["modified_map"] == {20: "2026-08-02T00:00:00Z"}


def test_opds_feed_passes_paging_context_for_the_next_link():
    from lenny.core.api import LennyAPI

    items = [_item(10, datetime(2026, 8, 1, tzinfo=timezone.utc))]
    with patch("lenny.core.api.Item.get_many", return_value=items), \
         patch("lenny.core.api.Item.count", return_value=99) as count, \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=_search_response(_fake_record(10, "Ten"))), \
         patch("lenny.core.api.LennyDataProvider.build_catalog", return_value={}) as build:
        LennyAPI.opds_feed(offset=0, limit=10, modified_since="2026-08-01")

    page = build.call_args.kwargs["page"]
    assert page == {
        "offset": 0,
        "limit": 10,
        "modified_since": "2026-08-01T00:00:00Z",
        "total": 99,
    }
    # The total must count the filtered set, not the whole table.
    assert count.call_args.kwargs["modified_since"] == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_empty_feed_still_carries_paging_context():
    """An empty page is a legitimate harvest result ("nothing changed"); it should
    still describe itself correctly rather than look like an unfiltered feed."""
    from lenny.core.api import LennyAPI

    with patch("lenny.core.api.Item.get_many", return_value=[]), \
         patch("lenny.core.api.Item.count", return_value=0), \
         patch("lenny.core.api.LennyDataProvider.empty_catalog", return_value={}) as empty:
        LennyAPI.opds_feed(limit=10, modified_since="2026-08-01")

    assert empty.call_args.kwargs["page"]["modified_since"] == "2026-08-01T00:00:00Z"


def test_single_publication_request_has_no_paging_context():
    from lenny.core.api import LennyAPI

    # A single-publication request reads its one row straight from `Item.exists`;
    # there is no `get_many` page behind it and so no count to take.
    item = _item(10, datetime(2026, 8, 1, tzinfo=timezone.utc))
    with patch("lenny.core.api.Item.exists", return_value=item), \
         patch("lenny.core.api.Loan.exists", return_value=None), \
         patch("lenny.core.api.Item.count") as count, \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=_search_response(_fake_record(10, "Ten"))), \
         patch("lenny.core.api.LennyDataProvider.build_publication", return_value={}) as build:
        LennyAPI.opds_feed(olid=10)

    count.assert_not_called()
    assert build.call_args.kwargs["modified_map"] == {10: "2026-08-01T00:00:00Z"}


# ---------------------------------------------------------------------------
# Route behaviour
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_client():
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


def test_route_rejects_a_malformed_modified_since(test_client):
    """A 400 tells the caller their filter is wrong. Letting it fall through would
    surface as a 503 (looks like Lenny is down) or, worse, as an unfiltered feed."""
    resp = test_client.get("/v1/api/opds?modified_since=notadate")
    assert resp.status_code == 400
    assert "modified_since" in resp.json()["detail"]


def test_route_forwards_a_parsed_modified_since(test_client):
    with patch("lenny.routes.api.LennyAPI.opds_feed", return_value={}) as feed:
        resp = test_client.get("/v1/api/opds?modified_since=2026-08-01&limit=5")

    assert resp.status_code == 200
    assert feed.call_args.kwargs["modified_since"] == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_route_without_modified_since_is_unfiltered(test_client):
    with patch("lenny.routes.api.LennyAPI.opds_feed", return_value={}) as feed:
        test_client.get("/v1/api/opds")

    assert feed.call_args.kwargs["modified_since"] is None


def test_items_route_rejects_a_malformed_modified_since(test_client):
    resp = test_client.get("/v1/api/items?modified_since=notadate")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# End-to-end: the contract openlibrary#13241's harvester actually consumes
# ---------------------------------------------------------------------------

def test_feed_satisfies_the_bookworm_harvester_contract():
    """One assertion per thing `openlibrary/bookworm/harvest.py` reads off a page:
    a followable `rel=next` that preserves the filter, and a parseable `modified`
    on every publication."""
    from lenny.core.api import LennyAPI
    from lenny.core.utils import parse_modified_since

    items = [
        _item(10, datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)),
        _item(20, datetime(2026, 8, 2, 6, 0, tzinfo=timezone.utc)),
    ]
    with patch("lenny.core.api.Item.get_many", return_value=items), \
         patch("lenny.core.api.Item.count", return_value=5), \
         patch("lenny.core.api.LennyDataProvider.search",
               return_value=_search_response(_fake_record(10, "Ten"), _fake_record(20, "Twenty"))):
        feed = LennyAPI.opds_feed(offset=0, limit=2, modified_since="2026-08-01")

    # Serializable, as the route requires.
    feed = json.loads(json.dumps(feed))

    # 1. rel=next exists (5 total, 2 shown) and keeps the filter.
    nxt = next(l["href"] for l in feed["links"] if l["rel"] == "next")
    assert "offset=2" in nxt and "limit=2" in nxt
    assert "modified_since=2026-08-01T00%3A00%3A00Z" in nxt

    # 2. Every publication carries a `modified` the harvester can parse into a
    #    cursor (`_as_utc(pub.modified)`).
    for pub in feed["publications"]:
        assert parse_modified_since(pub["metadata"]["modified"]).tzinfo is not None

    # 3. numberOfItems describes the whole filtered set, not this page.
    assert feed["metadata"]["numberOfItems"] == 5
