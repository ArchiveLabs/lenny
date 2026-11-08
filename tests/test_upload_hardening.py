"""Upload hardening (#244): EPUB-only, a real content check, one S3 object per
book, a per-edition lock, and uploads that do not stall the event loop.

The race tests need Postgres (the lock is a no-op on SQLite). They run when
LENNY_TEST_PG_URL points at a throwaway database and skip otherwise.
"""

import asyncio
import io
import os
import threading
import time
import zipfile
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

SECRET = "internal-secret-for-tests"


def make_epub(mimetype=b"application/epub+zip", with_mimetype=True, extra_entries=0):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if with_mimetype:
            zf.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", "<container/>")
        for i in range(extra_entries):
            zf.writestr(f"OEBPS/{i}.xhtml", "x")
    return buf.getvalue()


def upload_file(name="book.epub", data=None):
    from starlette.datastructures import Headers, UploadFile
    data = make_epub() if data is None else data
    return UploadFile(
        filename=name, file=io.BytesIO(data), size=len(data),
        headers=Headers({"content-type": "application/epub+zip"}),
    )


# ─── is_valid_epub ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("data, expected", [
    (make_epub(), True),
    (b"%PDF-1.7 renamed to .epub", False),
    (b"", False),
    (b"PK\x03\x04 truncated, no central directory", False),
    (make_epub(with_mimetype=False), False),
    (make_epub(mimetype=b"application/zip"), False),
    (make_epub(mimetype=b"application/epub+zip" + b"x" * 5000), False),
])
def test_is_valid_epub(data, expected):
    from lenny.core.utils import is_valid_epub
    assert is_valid_epub(io.BytesIO(data)) is expected


def test_is_valid_epub_rejects_too_many_entries_before_parsing_them():
    from lenny.core import utils
    data = make_epub(extra_entries=10)
    with patch.object(utils, "EPUB_MAX_ENTRIES", 5):
        assert utils.is_valid_epub(io.BytesIO(data)) is False
    assert utils.is_valid_epub(io.BytesIO(data)) is True


def test_corrupt_compressed_mimetype_is_rejected_not_raised():
    from lenny.core.utils import is_valid_epub
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip" * 50, compress_type=zipfile.ZIP_DEFLATED)
    data = bytearray(buf.getvalue())
    start = data.index(b"mimetype") + len(b"mimetype")
    for i in range(start, start + 12):       # scramble the start of the deflate stream
        data[i] ^= 0xFF
    assert is_valid_epub(io.BytesIO(bytes(data))) is False
    assert is_valid_epub(io.BytesIO(bytes(data[: len(data) // 2]))) is False


def test_is_valid_epub_leaves_the_file_at_the_start():
    from lenny.core.utils import is_valid_epub
    fp = io.BytesIO(make_epub())
    is_valid_epub(fp)
    assert fp.tell() == 0


# ─── upload_files / validate_files ───────────────────────────────────────────

def test_pdf_is_rejected_without_mentioning_pdf():
    from lenny.core.api import LennyAPI
    from lenny.core.exceptions import InvalidFileError
    with patch("lenny.core.api.s3") as s3:
        with pytest.raises(InvalidFileError) as e:
            LennyAPI.upload_files([upload_file("book.pdf", b"%PDF-1.7")], 1)
    assert "pdf" not in str(e.value).lower()
    s3.upload_fileobj.assert_not_called()


def test_renamed_non_epub_is_rejected_and_nothing_is_written():
    from lenny.core.api import LennyAPI
    from lenny.core.exceptions import InvalidFileError
    with patch("lenny.core.api.s3") as s3:
        with pytest.raises(InvalidFileError):
            LennyAPI.upload_files([upload_file("book.epub", b"%PDF-1.7")], 1)
    s3.upload_fileobj.assert_not_called()


@pytest.mark.parametrize("encrypt", [False, True])
def test_one_s3_object_per_book_whether_or_not_encrypted(encrypt):
    from lenny.core.api import LennyAPI
    with patch("lenny.core.api.s3") as s3:
        LennyAPI.upload_files([upload_file()], 777, encrypt=encrypt)
    assert s3.upload_fileobj.call_count == 1
    assert s3.upload_fileobj.call_args.args[2] == "777.epub"


def test_reupload_keeps_the_stored_copy_when_the_upload_fails():
    from lenny.core.api import LennyAPI
    from botocore.exceptions import ClientError
    from lenny.core.exceptions import S3UploadError
    with patch("lenny.core.api.s3") as s3, \
         patch("lenny.core.api.Item.exists", return_value=MagicMock()), \
         patch.object(LennyAPI, "_item_s3_keys", return_value=["5.epub", "5_encrypted.epub"]):
        s3.upload_fileobj.side_effect = ClientError({"Error": {"Message": "boom"}}, "put")
        with pytest.raises(S3UploadError):
            LennyAPI.reupload(5, [upload_file()])
    s3.delete_object.assert_not_called()


def test_reupload_removes_stale_keys_only_after_the_commit():
    from lenny.core.api import LennyAPI
    from lenny.core.exceptions import DatabaseUpdateError
    with patch("lenny.core.api.s3") as s3, patch("lenny.core.api.db") as db, \
         patch("lenny.core.api.Item.exists", return_value=MagicMock()), \
         patch.object(LennyAPI, "_item_s3_keys", return_value=["5.epub", "5_encrypted.epub"]):
        db.commit.side_effect = Exception("db exploded")
        with pytest.raises(DatabaseUpdateError):
            LennyAPI.reupload(5, [upload_file()])
    s3.delete_object.assert_not_called()


def test_reupload_validates_before_deleting_the_stored_copy():
    from lenny.core.api import LennyAPI
    from lenny.core.exceptions import InvalidFileError
    with patch("lenny.core.api.s3") as s3, \
         patch("lenny.core.api.Item.exists", return_value=MagicMock()), \
         patch.object(LennyAPI, "_item_s3_keys", return_value=["5.epub"]):
        with pytest.raises(InvalidFileError):
            LennyAPI.reupload(5, [upload_file("x.epub", b"not an epub")])
    s3.delete_object.assert_not_called()


def test_reupload_checks_size_before_deleting_the_stored_copy():
    from lenny.core.api import LennyAPI
    from lenny.core.exceptions import FileTooLargeError
    with patch("lenny.core.api.s3") as s3, \
         patch("lenny.core.api.Item.exists", return_value=MagicMock()), \
         patch.object(LennyAPI, "_item_s3_keys", return_value=["5.epub"]), \
         patch.object(LennyAPI, "MAX_FILE_SIZE", 10):
        with pytest.raises(FileTooLargeError):
            LennyAPI.reupload(5, [upload_file()])
    s3.delete_object.assert_not_called()


# ─── the lock ────────────────────────────────────────────────────────────────

def _fake_postgres_db():
    fake = MagicMock()
    fake.bind.dialect.name = "postgresql"
    return fake


def test_lock_editions_takes_distinct_locks_in_sorted_order():
    from lenny.core import models
    fake = _fake_postgres_db()
    with patch.object(models, "db", fake):
        models.lock_editions(30, 10, 30, 20)
    keys = [c.args[1]["key"] for c in fake.execute.call_args_list]
    assert len(keys) == 3 and len(set(keys)) == 3

    fake2 = _fake_postgres_db()
    with patch.object(models, "db", fake2):
        models.lock_editions(20, 30, 10)
    assert [c.args[1]["key"] for c in fake2.execute.call_args_list] == keys


def test_lock_editions_is_a_noop_off_postgres():
    from lenny.core import models
    fake = MagicMock()
    fake.bind.dialect.name = "sqlite"
    with patch.object(models, "db", fake):
        models.lock_editions(1)
    fake.execute.assert_not_called()


def test_add_always_ends_its_transaction_so_the_lock_is_released():
    from lenny.core.api import LennyAPI
    from lenny.core.exceptions import ItemExistsError
    with patch("lenny.core.api.db") as db, \
         patch("lenny.core.api.Item.exists", return_value=MagicMock()):
        with pytest.raises(ItemExistsError):
            LennyAPI.add(12, [upload_file()])
    db.rollback.assert_called()


@pytest.fixture
def pg():
    url = os.environ.get("LENNY_TEST_PG_URL")
    if not url:
        pytest.skip("set LENNY_TEST_PG_URL to a throwaway Postgres to run the race tests")
    from sqlalchemy import create_engine
    from lenny.core.db import Base, session
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session.remove()
    old = session.session_factory.kw.get("bind")
    session.configure(bind=engine)
    yield engine
    session.remove()
    session.configure(bind=old)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _race(n, olid):
    from lenny.core.api import LennyAPI
    from lenny.core.db import session
    from lenny.core.exceptions import ItemExistsError
    outcomes, barrier = [], threading.Barrier(n)

    def slow_upload(*a, **k):
        time.sleep(0.2)  # widen the window between the existence check and the insert

    def worker():
        try:
            barrier.wait()
            LennyAPI.add(olid, [upload_file()])
            outcomes.append("added")
        except ItemExistsError:
            outcomes.append("exists")
        finally:
            session.remove()

    with patch("lenny.core.api.s3") as s3:
        s3.upload_fileobj.side_effect = slow_upload
        threads = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    return outcomes


def _rows(engine, olid):
    from sqlalchemy import text
    with engine.connect() as c:
        return c.execute(text("select count(*) from items where openlibrary_edition = :o"), {"o": olid}).scalar()


def test_parallel_uploads_of_one_book_create_exactly_one_item(pg):
    outcomes = _race(8, 940000001)
    assert sorted(outcomes) == ["added"] + ["exists"] * 7
    assert _rows(pg, 940000001) == 1


def test_without_the_lock_the_same_race_duplicates_the_item(pg):
    """Control: proves the race is real, so the test above is not vacuous."""
    with patch("lenny.core.api.lock_editions"):
        outcomes = _race(8, 940000002)
    assert outcomes.count("added") > 1
    assert _rows(pg, 940000002) > 1


def test_parallel_uploads_of_different_books_all_succeed(pg):
    from lenny.core.api import LennyAPI
    from lenny.core.db import session
    results = []

    def worker(olid):
        try:
            LennyAPI.add(olid, [upload_file()])
            results.append(olid)
        finally:
            session.remove()

    with patch("lenny.core.api.s3"):
        threads = [threading.Thread(target=worker, args=(940000100 + i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert len(results) == 6


# ─── the route: off the event loop, and bounded ──────────────────────────────

@pytest.fixture(scope="module")
def app_module():
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield app


@pytest.fixture
def admin_headers():
    from lenny.core import auth
    with patch.object(auth, "ADMIN_INTERNAL_SECRET", SECRET), \
         patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        yield {
            "X-Admin-Internal-Secret": SECRET,
            "Authorization": f"Bearer {auth.issue_admin_token()}",
        }


def _post(client, headers, olid):
    return client.post(
        "/v1/api/upload", headers=headers,
        data={"openlibrary_edition": str(olid), "encrypted": "false"},
        files={"file": ("book.epub", make_epub(), "application/epub+zip")},
    )


@pytest.mark.anyio
async def test_a_slow_upload_does_not_stall_other_requests(app_module, admin_headers):
    import httpx
    main_thread = threading.get_ident()
    ran_on = []

    def slow_add(**kwargs):
        ran_on.append(threading.get_ident())
        time.sleep(0.6)

    transport = httpx.ASGITransport(app=app_module)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        with patch("lenny.routes.api.LennyAPI.add", side_effect=slow_add):
            upload = asyncio.create_task(_post(client, admin_headers, 950000001))
            await asyncio.sleep(0.1)
            started = time.monotonic()
            health = await client.get("/v1/api/health")
            elapsed = time.monotonic() - started
            assert (await upload).status_code == 200
    assert health.status_code == 200
    assert elapsed < 0.3, f"health took {elapsed:.2f}s while an upload was in flight"
    assert ran_on and ran_on[0] != main_thread


@pytest.mark.anyio
async def test_uploads_run_no_more_at_once_than_the_limit(app_module, admin_headers):
    import anyio
    import httpx
    lock, running, peak = threading.Lock(), [0], [0]

    def slow_add(**kwargs):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.15)
        with lock:
            running[0] -= 1

    transport = httpx.ASGITransport(app=app_module)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        with patch("lenny.routes.api.LennyAPI.add", side_effect=slow_add), \
             patch("lenny.routes.api._UPLOAD_LIMITER", anyio.CapacityLimiter(2)):
            responses = await asyncio.gather(
                *[_post(client, admin_headers, 950000100 + i) for i in range(6)]
            )
    assert [r.status_code for r in responses] == [200] * 6
    assert peak[0] == 2
