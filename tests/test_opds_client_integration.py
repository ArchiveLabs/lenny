"""Built-in OPDS client: the OAuth hand-off it relies on, and the OTP hardening
that shipped with it (#242, #259, #262)."""

import asyncio
import base64
import hashlib
import os
import threading
from unittest.mock import patch

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "test-seed-for-unit-tests-only-32b!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.api import LennyAPI  # noqa: E402
from lenny.core.cache import Cache  # noqa: E402
from lenny.core.db import Base, engine, run_blocking  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import ensure_default_clients  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("cache", "oauth_access_tokens", "oauth_authorization_codes", "oauth_clients"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
            db.commit()
        except Exception:
            db.rollback()
    db.remove()


def _challenge() -> str:
    digest = hashlib.sha256(b"v" * 43).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _authorize(redirect_uri: str):
    ensure_default_clients()
    return TestClient(app).get("/v1/api/oauth2/authorize", params={
        "response_type": "code", "client_id": "lenny-opds-client",
        "redirect_uri": redirect_uri, "state": "s", "scope": "openid",
        "code_challenge": _challenge(), "code_challenge_method": "S256",
    }, follow_redirects=False)


# ─── the client can start a sign-in on its own node ──────────────────────────

def test_authorize_accepts_the_built_in_client_at_its_own_origin():
    resp = _authorize(LennyAPI.make_url(""))
    # Not an error page: it goes on to sign the patron in.
    assert resp.status_code in (302, 303), resp.text
    assert "error" not in resp.headers["location"].split("?")[0]


def test_authorize_refuses_the_built_in_client_at_any_other_origin():
    resp = _authorize("https://evil.example.org")
    assert resp.status_code == 400


# ─── #242: the guess cap is per mailbox, not per spelling ────────────────────

def test_verify_cap_ignores_letter_case_and_spaces():
    for _ in range(auth.ATTEMPT_LIMIT):
        assert auth.OTP.is_rate_limited("Victim@Example.com") is False
    assert auth.OTP.is_rate_limited("victim@example.com") is True
    assert auth.OTP.is_rate_limited("  VICTIM@example.com ") is True


# ─── #259: a broken cache must not switch the OTP caps off ───────────────────

def test_throttle_fails_closed_for_otp_when_the_cache_errors():
    with patch("lenny.core.cache.db.query", side_effect=RuntimeError("db down")):
        assert Cache.is_throttled("otp:send", "k", 1, 60, fail_closed=True) is True
        assert auth.OTP.is_send_rate_limited("a@example.com") is True
        assert auth.OTP.is_rate_limited("a@example.com") is True


def test_throttle_stays_open_for_scopes_that_prefer_availability():
    with patch("lenny.core.cache.db.query", side_effect=RuntimeError("db down")):
        assert Cache.is_throttled("imports", "k", 1, 60) is False


# ─── #262: blocking work runs off the event loop and cleans up its session ───

def test_run_blocking_uses_another_thread_and_clears_that_threads_session():
    seen = {}

    def work():
        seen["thread"] = threading.get_ident()
        return "ok"

    with patch("lenny.core.db.session.remove") as remove:
        assert asyncio.run(run_blocking(work)) == "ok"
    assert seen["thread"] != threading.get_ident()
    remove.assert_called_once()


def test_run_blocking_clears_the_session_even_when_the_call_raises():
    def boom():
        raise ValueError("x")

    with patch("lenny.core.db.session.remove") as remove:
        with pytest.raises(ValueError):
            asyncio.run(run_blocking(boom))
    remove.assert_called_once()
