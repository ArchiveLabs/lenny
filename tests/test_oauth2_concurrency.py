"""Concurrency tests for the single-use guarantees.

These need real Postgres. Under SQLite the suite runs one in-memory database per
connection, so two "concurrent" redemptions never contend and a double-spend is
structurally invisible — which is exactly how the races below survived 74
passing tests.

    docker run -d --name lenny_oauth2_testdb \
      -e POSTGRES_DB=lenny_test -e POSTGRES_USER=lenny -e POSTGRES_PASSWORD=lennytest \
      -p 127.0.0.1:55432:5432 postgres:16

    TESTING=false DB_USER=lenny DB_PASSWORD=lennytest DB_HOST=127.0.0.1 \
    DB_PORT=55432 DB_NAME=lenny_test pytest tests/test_oauth2_concurrency.py

Skipped automatically when DB_URI is not Postgres.

Each test drives two real threads through the real code paths, synchronised on a
`threading.Barrier` so both pass their validity checks before either writes.
That is the interleaving READ COMMITTED permits and a production node with
three uvicorn workers will hit.
"""

import base64
import hashlib
import threading

import pytest

from lenny.configs import DB_URI

pytestmark = pytest.mark.skipif(
    not DB_URI.startswith("postgresql"),
    reason="needs real Postgres; see this module's docstring")

from sqlalchemy import text  # noqa: E402

from lenny.core import oauth2  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import AccessToken, AuthorizationCode, OAuthClient  # noqa: E402

REDIRECT = "https://consumer.example.org/callback"
PATRON = "concurrency-patron-hash"


@pytest.fixture(autouse=True)
def clean_tables():
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_access_tokens", "oauth_authorization_codes", "oauth_clients"):
        db.execute(text(f"DELETE FROM {table}"))
    db.commit()
    db.remove()


@pytest.fixture
def client():
    obj, _ = OAuthClient.register(
        name="Racer", redirect_uris=[REDIRECT], scopes=["loans:read", "borrow"])
    return obj


def pkce():
    verifier = "v" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def run_concurrently(fn, n=2):
    """Run `fn` in `n` threads released together by a barrier.

    Each thread gets its own database connection for free: `oauth2.db` is a
    SQLAlchemy `scoped_session`, which is thread-local, so the threads contend
    in Postgres rather than sharing one session and serialising.

    The barrier is the point — it lines both callers up past their validity
    checks before either writes, which is the interleaving READ COMMITTED
    permits and a node running three uvicorn workers will hit.
    """
    barrier = threading.Barrier(n)
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        try:
            barrier.wait(timeout=20)
            out = fn()
            with lock:
                results.append(out)
        except Exception as exc:                      # pragma: no cover - diagnostics
            with lock:
                errors.append(exc)
        finally:
            oauth2.db.remove()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"worker raised: {errors}"
    return results


class TestAuthorizationCodeIsSingleUse:
    def test_a_code_cannot_be_redeemed_twice_concurrently(self, client):
        """RFC 6749 §4.1.2: a code must be usable exactly once.

        Check-then-write is not enough under READ COMMITTED — both callers read
        `redeemed_at IS NULL`, both pass, both write. An attacker who intercepts
        a code and races the legitimate client gets a live token pair, and
        because neither redemption saw the row as spent, reuse detection never
        fires. The claim must be atomic.
        """
        verifier, challenge = pkce()
        code = AuthorizationCode.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            redirect_uri=REDIRECT, scope="loans:read", code_challenge=challenge)

        def redeem():
            row, err = AuthorizationCode.redeem(
                code, client_id=client.client_id, redirect_uri=REDIRECT,
                code_verifier=verifier)
            return err is None

        outcomes = run_concurrently(redeem)
        assert sum(outcomes) == 1, (
            f"a single-use code was redeemed {sum(outcomes)} times")


class TestRefreshTokenRotation:
    def test_a_refresh_token_cannot_rotate_twice_concurrently(self, client):
        """Rotation only protects anyone if a stolen refresh token and the
        legitimate one cannot both succeed. Two parallel requests must not
        produce two independent token families."""
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")

        def rotate():
            issued, err = AccessToken.refresh(refresh, client_id=client.client_id)
            return err is None

        outcomes = run_concurrently(rotate)
        assert sum(outcomes) == 1, (
            f"one refresh token minted {sum(outcomes)} token families")
