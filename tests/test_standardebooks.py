#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    tests.test_standardebooks
    ~~~~~~~~~~~~~~~~~~~~~~~~~

    Tests the Standard Ebooks importer — chiefly that it skips books already in
    the library, which is what makes a batch limit mean "N books I don't have"
    and lets the admin import button be pressed repeatedly.

    :copyright: (c) 2015 by Authors.
    :license: see LICENSE for more details.
"""

import io
from unittest.mock import patch

import pytest

pytest.importorskip("httpx")

from lenny.core.standardebooks import import_standardebooks
from lenny.core.imports import ImportJob, DONE, FAILED

EPUB = io.BytesIO(b'PK\x03\x04' + b'0' * 128)


class FakeOLID(str):
    """Mirrors OpenLibrary.olid's nested OpenLibraryID: "OL123M" but int() -> 123.

    Replicated rather than imported because the real class is defined inside the
    `olid` property. The importer relies on that int() behaviour, so a plain str
    here would silently test nothing.
    """
    def __int__(self):
        return int(self.strip("OLM"))


class FakeRecord:
    """Stands in for an OpenLibraryRecord: only .olid and .standardebooks_id are used."""

    def __init__(self, olid, se_id="author/title"):
        self.olid = FakeOLID(olid)
        self.standardebooks_id = se_id


@pytest.fixture
def db():
    from lenny.core.db import engine, session
    from lenny.core.cache import CacheEntry

    CacheEntry.__table__.create(engine, checkfirst=True)
    yield session
    session.rollback()
    CacheEntry.__table__.drop(engine, checkfirst=True)


def _run(records, existing=(), **kwargs):
    """Run the importer over `records` with `existing` already in the catalog."""
    with patch("lenny.core.standardebooks.OpenLibrary.search", return_value=iter(records)), \
         patch("lenny.core.standardebooks.Item.get_all", return_value={o: object() for o in existing}), \
         patch("lenny.core.standardebooks.StandardEbooks.download",
               side_effect=lambda *a, **k: io.BytesIO(EPUB.getvalue())), \
         patch("lenny.core.standardebooks.LennyClient.upload", return_value=True) as upload:
        stats = import_standardebooks(**kwargs)
    return stats, upload


def test_skips_books_already_in_the_library(db):
    """Without this, `limit` is spent re-fetching books we already have."""
    records = [FakeRecord("OL1M"), FakeRecord("OL2M"), FakeRecord("OL3M")]
    stats, upload = _run(records, existing=(1, 2))

    assert stats["uploaded"] == 1
    assert stats["skipped"] == 2
    assert [call.args[0] for call in upload.call_args_list] == [3]


def test_limit_counts_new_books_not_records_scanned(db):
    records = [FakeRecord(f"OL{i}M") for i in range(1, 11)]
    stats, upload = _run(records, existing=(1, 2, 3), limit=2)

    assert stats["uploaded"] == 2
    assert [call.args[0] for call in upload.call_args_list] == [4, 5]


def test_a_repeat_run_picks_up_where_the_last_left_off(db):
    """Pressing "import 2 more" twice must import 4 distinct books, not the same 2."""
    records = [FakeRecord(f"OL{i}M") for i in range(1, 11)]
    first, upload_a = _run(records, existing=(), limit=2)

    imported = [call.args[0] for call in upload_a.call_args_list]
    records = [FakeRecord(f"OL{i}M") for i in range(1, 11)]
    second, upload_b = _run(records, existing=imported, limit=2)

    assert first["uploaded"] == second["uploaded"] == 2
    assert [call.args[0] for call in upload_b.call_args_list] == [3, 4]


def test_progress_is_recorded_under_the_standardebooks_source(db):
    _run([FakeRecord("OL7M")], existing=())

    records = ImportJob.list()
    assert len(records) == 1
    assert records[0]["source"] == "standardebooks"
    assert records[0]["status"] == DONE


def test_books_missing_from_the_preload_set_are_recorded(db):
    with patch("lenny.core.standardebooks.OpenLibrary.search", return_value=iter([FakeRecord("OL9M")])), \
         patch("lenny.core.standardebooks.Item.get_all", return_value={}), \
         patch("lenny.core.standardebooks.StandardEbooks.download", return_value=None):
        stats = import_standardebooks()

    assert stats["not_in_set"] == 1
    assert ImportJob.list()[0]["status"] == FAILED


def test_standardebooks_are_imported_as_open_access(db):
    _, upload = _run([FakeRecord("OL7M")], existing=())
    assert upload.call_args.kwargs["encrypted"] is False


# --- route ------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield TestClient(app)


@pytest.fixture
def admin_ok():
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True), \
         patch("lenny.routes.api.Cache.is_throttled", return_value=False):
        yield


def test_import_requires_admin(client):
    assert client.post("/v1/api/admin/imports/standardebooks", json={}).status_code == 403


def test_import_starts_in_the_background(client, admin_ok):
    with patch("lenny.routes.api.import_standardebooks") as importer:
        response = client.post("/v1/api/admin/imports/standardebooks", json={"limit": 10})

    assert response.status_code == 200
    assert response.json() == {"source": "standardebooks", "limit": 10, "status": "started"}
    importer.assert_called_once_with(10)


def test_import_defaults_and_caps_the_limit(client, admin_ok):
    with patch("lenny.routes.api.import_standardebooks"):
        assert client.post("/v1/api/admin/imports/standardebooks", json={}).json()["limit"] == 25
        assert client.post(
            "/v1/api/admin/imports/standardebooks", json={"limit": 99999}
        ).json()["limit"] == 100


def test_import_rejects_a_nonsense_limit(client, admin_ok):
    with patch("lenny.routes.api.import_standardebooks"):
        assert client.post("/v1/api/admin/imports/standardebooks", json={"limit": "lots"}).status_code == 400
        assert client.post("/v1/api/admin/imports/standardebooks", json={"limit": 0}).status_code == 400


def test_a_second_concurrent_run_is_refused(client):
    """Two runs would race on the same "already have" set and double-download."""
    with patch("lenny.routes.api.auth.verify_admin_internal_secret", return_value=True), \
         patch("lenny.routes.api.auth.verify_admin_token", return_value=True), \
         patch("lenny.routes.api.Cache.is_throttled", return_value=True), \
         patch("lenny.routes.api.import_standardebooks") as importer:
        response = client.post("/v1/api/admin/imports/standardebooks", json={})

    assert response.status_code == 409
    importer.assert_not_called()
