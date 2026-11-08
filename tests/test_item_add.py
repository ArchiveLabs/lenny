"""Regression test for LennyAPI.add() — the actual new-book upload path.

A prior revert removed Item.title/author but missed this call site, which
still passed title=/author= into the Item constructor. Every upload would
TypeError inside the try/except, roll back the DB insert, but leave the
already-uploaded S3 files orphaned (upload_files runs before the failing
constructor call, outside its own transaction).
"""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")


@pytest.fixture(autouse=True)
def fresh_tables():
    from lenny.core.db import Base, engine
    Base.metadata.create_all(engine)
    yield


def test_add_constructs_item_without_title_or_author():
    from lenny.core.api import LennyAPI
    from lenny.core.db import session as db
    from lenny.core.models import FormatEnum

    fake_file = MagicMock()
    fake_file.filename = "test.epub"

    with patch.object(LennyAPI, "upload_files", return_value=FormatEnum.EPUB.value):
        item = LennyAPI.add(
            openlibrary_edition=920000001, files=[fake_file], encrypt=False,
        )

    assert item.openlibrary_edition == 920000001
    assert item.encrypted is False
    assert item.formats == FormatEnum.EPUB
    assert not hasattr(item, "title")
    assert not hasattr(item, "author")

    db.delete(item)
    db.commit()
