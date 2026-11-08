"""Admin lists carry available_copies and num_lendable_total, from one query for the page."""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")


@pytest.fixture
def library():
    from lenny.core.db import Base, engine, session
    from lenny.core.models import FormatEnum, Item, Loan
    Base.metadata.create_all(engine)
    session.remove()
    items = [Item(openlibrary_edition=o, formats=FormatEnum.EPUB) for o in (101, 102, 103)]
    session.add_all(items); session.commit()
    yield session, items
    session.rollback()
    session.query(Loan).delete(); session.query(Item).delete(); session.commit()
    session.remove()


def loan(session, item, **kw):
    from lenny.core.models import Loan
    row = Loan(item_id=item.id, patron_email_hash="h" + str(item.id) + str(kw), due_date=kw.pop("due", datetime.now(timezone.utc) + timedelta(days=14)), **kw)
    session.add(row); session.commit()
    return row


def test_available_map_counts_active_loans_in_one_query(library):
    from sqlalchemy import event
    from lenny.core.db import engine
    from lenny.core.models import Item
    session, (a, b, c) = library
    loan(session, a)                                                                   # on loan
    loan(session, b, returned_at=datetime.now(timezone.utc))                           # returned: free
    loan(session, c, due=datetime.now(timezone.utc) - timedelta(days=1))               # overdue (expired): free
    statements = []
    def record(conn, cursor, statement, *rest): statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        got = Item.available_map([a, b, c, None])
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert got == {a.id: 0, b.id: 1, c.id: 1}
    assert len([s for s in statements if "FROM loans" in s]) == 1                       # one loans query for the whole page
    assert all(got[i.id] == i.available_copies for i in (a, b, c))                      # and it agrees with the per-item property
    assert Item.available_map([]) == {} and Item.available_map([None]) == {}


def test_available_map_falls_back_to_available_when_the_database_errors(library):
    from lenny.core.models import Item
    session, (a, *_rest) = library
    with patch("lenny.core.models.db.query", side_effect=RuntimeError("down")):
        assert Item.available_map([a]) == {}


def test_enriched_records_carry_the_copies(library):
    from lenny.core.api import LennyAPI
    from lenny.core.openlibrary import OpenLibraryRecord
    session, (a, b, _c) = library
    loan(session, a)
    enriched = {101: OpenLibraryRecord({"title": "A", "lenny": a}), 102: OpenLibraryRecord({"title": "B", "lenny": b})}
    out = LennyAPI.with_availability(enriched)
    assert (out[101]["available_copies"], out[101]["num_lendable_total"]) == (0, 1)
    assert (out[102]["available_copies"], out[102]["num_lendable_total"]) == (1, 1)
    assert LennyAPI.with_availability({}) == {}
    assert LennyAPI.with_availability({5: "not a record"}) == {5: "not a record"}


def test_search_results_carry_the_copies(library):
    from lenny.core.api import LennyAPI
    from lenny.core.openlibrary import OpenLibraryRecord
    session, (a, b, _c) = library
    loan(session, a)
    hits = [OpenLibraryRecord({"title": "A", "author_name": ["X"], "editions": {"docs": [{"key": "/books/OL101M"}]}}),
            OpenLibraryRecord({"title": "B", "author_name": [], "editions": {"docs": [{"key": "/books/OL102M"}]}})]
    with patch("lenny.core.api.OpenLibrary.search", return_value=hits):
        items, unavailable = LennyAPI.admin_search_items(q="x")
    got = {i["edition_key"]: (i["available_copies"], i["num_lendable_total"]) for i in items}
    assert got == {"OL101M": (0, 1), "OL102M": (1, 1)} and unavailable is False


def test_the_admin_list_route_includes_them(library):
    from fastapi.testclient import TestClient
    from lenny.core.openlibrary import OpenLibraryRecord
    session, (a, b, _c) = library
    loan(session, a)
    enriched = {101: OpenLibraryRecord({"title": "A", "lenny": a}), 102: OpenLibraryRecord({"title": "B", "lenny": b})}
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True), \
         patch("lenny.core.api.LennyAPI.get_enriched_items", return_value=enriched):
        body = TestClient(app).get("/v1/api/admin/items", headers={"X-Admin-Internal-Secret": "x", "Authorization": "Bearer t"}).json()
    assert body["101"]["available_copies"] == 0 and body["102"]["available_copies"] == 1 and body["102"]["num_lendable_total"] == 1
