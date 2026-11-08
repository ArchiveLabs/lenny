"""The read gate must not accept a caller-supplied identity.

`/items/{id}/read` and `/items/{id}/readium/*` take an `email` parameter in
their signature, and FastAPI binds an untyped parameter from the query string.
If that value were trusted, anyone who knew a patron's address could read any
book that patron has on loan — no session, no password, no OTP.

It is not trusted: `requires_item_auth` overwrites `email` with whatever
`auth_check` resolved from the session cookie, before it checks for an error.
Nothing else in the suite pins that, and the question was raised from outside
by someone reading the route signature — which is exactly how it looks.
"""

import os

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "read-gate-test-seed-32-chars-okay")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

PATRON = "patron@example.org"


@pytest.fixture
def borrowed_item():
    """An encrypted item that PATRON genuinely has on loan.

    Ids are explicit because `Item.id` is a BigInteger, which SQLite does not
    autoincrement — the same divergence documented in the concurrency suite.
    """
    Base.metadata.create_all(engine)
    db.query(Loan).delete()
    db.query(Item).delete()
    db.commit()
    item = Item(id=1, openlibrary_edition=99001, encrypted=True,
                formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    db.add(Loan(id=1, item_id=item.id, patron_email_hash=hash_email(PATRON)))
    db.commit()
    yield item
    db.query(Loan).delete()
    db.query(Item).delete()
    db.commit()
    db.remove()


@pytest.mark.parametrize("path", [
    "/v1/api/items/99001/read",
    "/v1/api/items/99001/readium/manifest.json",
])
def test_attack_query_email_does_not_open_someone_elses_loan(borrowed_item, path):
    client = TestClient(app, follow_redirects=False)

    assert client.get(path).status_code == 401, "unauthenticated read was allowed"

    # The attacker knows the address and there IS a live loan for it. The only
    # thing missing is the session, and that has to be enough.
    r = client.get(path, params={"email": PATRON})
    assert r.status_code == 401, (
        f"{path} honoured a caller-supplied email: knowing a patron's address "
        "was enough to read their loan")
