"""/shelf accepts the OAuth2 bearer token a reading app gets from the PKCE flow.

A reading app (e.g. the Book Server) holds an access token, not Lenny's login
cookie. /shelf only understood the cookie, so the app's bookshelf page said
"Not signed in" even with a perfectly valid token.
"""

import os
from unittest.mock import patch

import pytest
import requests
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core.api import LennyAPI  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.models import FormatEnum, Item, Loan  # noqa: E402
from lenny.core.oauth2 import AccessToken, OAuthClient  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

ME = "shelf-patron@example.com"
SOMEONE_ELSE = "someone-else@example.com"
EDITION = 910000777


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_access_tokens", "oauth_clients", "loans", "items"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


def token_for(email, scope="loans:read"):
    client, _ = OAuthClient.register(
        name="Reader", redirect_uris=["opds://test"], scopes=[scope], is_confidential=False)
    access, _refresh, _row = AccessToken.issue(
        client_id=client.client_id, patron_email_hash=hash_email(email), scope=scope)
    return access


def lend_to(email):
    item = Item(openlibrary_edition=EDITION, encrypted=True, formats=FormatEnum.EPUB)
    db.add(item)
    db.commit()
    db.add(Loan(item_id=item.id, patron_email_hash=hash_email(email)))
    db.commit()


def shelf(token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(app).get("/v1/api/shelf", headers=headers)


def test_without_credentials_the_shelf_is_a_401_with_the_auth_document():
    r = shelf()
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/opds-authentication+json")


def test_a_valid_token_is_signed_in_and_sees_its_own_loans():
    lend_to(ME)
    with patch.object(LennyAPI, "get_shelf_feed", wraps=LennyAPI.get_shelf_feed) as spy, \
         patch("lenny.core.api.LennyDataProvider.search", side_effect=requests.exceptions.ConnectionError("no network")):
        r = shelf(token_for(ME))
    # The feed builder was handed the patron's hash, flagged as already hashed.
    args, kwargs = spy.call_args
    assert args[0] == hash_email(ME) and kwargs["hashed"] is True
    # Open Library being unreachable degrades to an empty shelf, not a sign-in prompt.
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/opds+json")


def test_the_loan_lookup_accepts_a_hashed_identity():
    lend_to(ME)
    assert [l.openlibrary_edition for l in LennyAPI.get_borrowed_items(hash_email(ME), hashed=True)] == [EDITION]
    assert [l.openlibrary_edition for l in LennyAPI.get_borrowed_items(ME)] == [EDITION]


def test_attack_a_token_only_ever_sees_its_own_patrons_loans():
    lend_to(ME)
    assert LennyAPI.get_borrowed_items(hash_email(SOMEONE_ELSE), hashed=True) == []


def test_attack_a_token_without_loans_read_is_refused():
    assert shelf(token_for(ME, scope="borrow")).status_code == 401


def test_attack_a_made_up_token_is_refused():
    assert shelf("not-a-real-token").status_code == 401


def test_attack_a_revoked_client_cuts_the_token_off():
    token = token_for(ME)
    client_id = AccessToken.authenticate(token).client_id
    OAuthClient.disable(client_id)
    assert shelf(token).status_code == 401


# ─── /profile and the cookie login (implicit), for both flows ────────────────

def cookie_for(email):
    from lenny.core import auth
    return auth.create_session_cookie(email)


def get(path, token=None, cookie=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    cookies = {"session": cookie} if cookie else {}
    return TestClient(app).get(f"/v1/api/{path}", headers=headers, cookies=cookies)


def test_profile_without_credentials_is_a_401():
    assert get("profile").status_code == 401


def test_profile_for_a_cookie_login_keeps_name_and_email():
    """The implicit flow's patron is unchanged by any of this."""
    lend_to(ME)
    r = get("profile", cookie=cookie_for(ME))
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["email"] == ME and meta["name"] == "shelf-patron"
    from lenny.configs import LOAN_LIMIT
    assert r.json()["loans"]["available"] == max(0, LOAN_LIMIT - 1)


def test_profile_for_a_bearer_token_counts_loans_but_shows_no_address():
    lend_to(ME)
    r = get("profile", token=token_for(ME))
    assert r.status_code == 200
    body = r.json()
    from lenny.configs import LOAN_LIMIT
    assert body["loans"]["available"] == max(0, LOAN_LIMIT - 1)
    assert "email" not in body["metadata"] and "name" not in body["metadata"]
    assert hash_email(ME) not in r.text, "the stored hash must never be shown as an address"


def test_attack_profile_token_without_loans_read_is_refused():
    assert get("profile", token=token_for(ME, scope="borrow")).status_code == 401


def test_attack_profile_token_never_counts_another_patrons_loans():
    lend_to(ME)
    from lenny.configs import LOAN_LIMIT
    r = get("profile", token=token_for(SOMEONE_ELSE))
    assert r.json()["loans"]["available"] == LOAN_LIMIT


def test_shelf_still_works_with_a_cookie_login():
    with patch("lenny.core.api.LennyDataProvider.search", side_effect=requests.exceptions.ConnectionError("x")):
        r = get("shelf", cookie=cookie_for(ME))
    assert r.status_code == 200
