"""The patron identity hash is keyed with LENNY_EMAIL_HASH_KEY, and the migration
that keys existing rows keeps every patron's loans and OAuth grants reachable."""

import hashlib
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")
os.environ.setdefault("LENNY_EMAIL_HASH_KEY", "test-email-hash-key-for-unit-tests")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

from lenny import configs  # noqa: E402
from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.oauth2 import AccessToken, AuthorizationCode, OAuthClient, SessionTicket, _hash  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402
from lenny.routes.api import get_authenticated_identity  # noqa: E402

ME = "keyed-patron@example.com"
EDITION = 910888001
READ_PATH = f"/v1/api/items/{EDITION}/read"
MIGRATION = pathlib.Path(__file__).parent.parent / "alembic/versions/e2b4c6d8f0a1_key_patron_email_hash.py"


def old_hash(email):
    """What Lenny stored before the hash was keyed."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def load_migration():
    spec = importlib.util.spec_from_file_location("key_patron_email_hash", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def request_with(cookie_value):
    return Request({
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(b"cookie", f"session={cookie_value}".encode())],
        "client": ("testclient", 12345), "scheme": "http", "server": ("testserver", 80),
    })


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_session_tickets", "oauth_authorization_codes", "oauth_access_tokens",
                  "oauth_clients", "loans", "items"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


# ── hash_email ───────────────────────────────────────────────────────────────

def test_same_email_same_hash_and_normalized():
    assert hash_email(ME) == hash_email(ME)
    assert hash_email("  Keyed-Patron@Example.COM \n") == hash_email(ME)


def test_hash_is_keyed():
    h = hash_email(ME)
    assert len(h) == 64 and int(h, 16) >= 0
    assert h != old_hash(ME), "an unkeyed sha256 must no longer be the identity"


def test_a_different_key_gives_a_different_hash(monkeypatch):
    h = hash_email(ME)
    monkeypatch.setattr(configs, "EMAIL_HASH_KEY", "some-other-install-key")
    assert hash_email(ME) != h


def test_startup_fails_without_the_key():
    env = {k: v for k, v in os.environ.items() if k != "LENNY_EMAIL_HASH_KEY"}
    r = subprocess.run([sys.executable, "-c", "import lenny.configs"], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "LENNY_EMAIL_HASH_KEY is not set" in r.stderr


# ── stale handoff cookies ────────────────────────────────────────────────────

def test_a_hash_cookie_from_before_the_key_is_signed_out():
    stale = auth._get_serializer().dumps({"patron_email_hash": old_hash(ME), "ip": "testclient"})
    assert get_authenticated_identity(request_with(stale), session=stale) == (None, False)
    fresh = auth.create_hashed_session_cookie(hash_email(ME), "testclient")
    assert get_authenticated_identity(request_with(fresh), session=fresh) == (hash_email(ME), True)


# ── migration ────────────────────────────────────────────────────────────────

def test_migration_keeps_loans_and_grants_reachable():
    old = old_hash(ME)
    item = Item(openlibrary_edition=EDITION, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    db.add(Loan(item_id=item.id, patron_email_hash=old))
    client, _ = OAuthClient.register(name="Reader", redirect_uris=["opds://test"],
                                     scopes=["loans:read", "borrow"], is_confidential=False)
    code = AuthorizationCode.issue(client_id=client.client_id, patron_email_hash=old,
                                   redirect_uri="opds://test", scope="loans:read borrow",
                                   code_challenge="x" * 43)
    access, _refresh, _row = AccessToken.issue(client_id=client.client_id,
                                               patron_email_hash=old, scope="loans:read borrow")
    ticket = SessionTicket.issue(client_id=client.client_id, patron_email_hash=old, scope="borrow")
    db.commit()
    assert not Loan.exists(item.id, ME), "precondition: old rows don't match the keyed hash"

    assert load_migration().rehash(db.connection(), configs.EMAIL_HASH_KEY.encode()) == 1
    db.commit()
    db.expire_all()

    # Cookie (plaintext email) path.
    assert Loan.exists(item.id, ME)
    # Code and bearer token now carry the keyed hash.
    assert db.query(AuthorizationCode).filter_by(code_hash=_hash(code)).one().patron_email_hash == hash_email(ME)
    assert AccessToken.authenticate(access).patron_email_hash == hash_email(ME)
    r = TestClient(app).get("/v1/api/oauth2/loans", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200, r.text
    assert len(r.json()["loans"]) == 1
    # Session handoff from a pre-migration ticket resolves to the same patron.
    r = TestClient(app).get("/v1/api/oauth2/session", params={"ticket": ticket, "redir": READ_PATH},
                            follow_redirects=False)
    assert r.status_code == 303, r.text
    value = r.cookies.get("session") or next(
        v.split(";", 1)[0][len("session="):] for k, v in r.headers.multi_items()
        if k.lower() == "set-cookie" and v.startswith("session="))
    assert get_authenticated_identity(request_with(value), session=value) == (hash_email(ME), True)
    # Return and borrow again under the same identity.
    db.expire_all()
    item = db.query(Item).filter_by(openlibrary_edition=EDITION).one()
    item.unborrow(ME)
    assert not Loan.exists(item.id, ME)
    item.borrow(ME)
    assert Loan.exists(item.id, hash_email(ME), hashed=True)
