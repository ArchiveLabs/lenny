"""Concurrency tests for the single-use guarantees.

These need real Postgres. Under SQLite the whole suite shares one connection
(`core/db.py` pins `StaticPool` for `:memory:`, because otherwise each checkout
gets its own empty database), so two "concurrent" redemptions are serialised by
the connection itself and a double-spend is structurally invisible — which is
exactly how the races below survived 74 passing tests. `SELECT ... FOR UPDATE`
is also a no-op there, so the lock ordering these tests pin cannot be observed
on SQLite at all.

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
    for table in ("oauth_access_tokens", "oauth_authorization_codes", "oauth_clients",
                  "loans", "items"):
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


    def test_reuse_detection_leaves_no_live_token(self, client):
        """Detecting a replay must actually kill the grant.

        Claiming the code atomically is only half the job. The loser trips reuse
        detection and revokes the family; the winner then *issues*. If issuing
        merely reads the grant, it reads a snapshot taken before the loser's
        revocation committed and inserts a live token against a grant the server
        has recorded as revoked — so roughly half of all detected replays handed
        the attacker a working token for the full hour, silently, because the
        honest client's call still returned 200.

        `issue` therefore takes `SELECT ... FOR UPDATE` on the grant, which
        serialises it against `revoke_for_code`. Either order is fine; what must
        never happen is a usable token surviving.
        """
        verifier, challenge = pkce()
        code = AuthorizationCode.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            redirect_uri=REDIRECT, scope="loans:read", code_challenge=challenge)

        def redeem_and_issue():
            """What the token endpoint does: claim, then mint."""
            row, err = AuthorizationCode.redeem(
                code, client_id=client.client_id, redirect_uri=REDIRECT,
                code_verifier=verifier)
            if err:
                return None
            try:
                access, _, _ = AccessToken.issue(
                    client_id=client.client_id,
                    patron_email_hash=row.patron_email_hash,
                    scope=row.scope, authorization_code_id=row.id)
            except oauth2.GrantRevoked:
                return None
            return access

        minted = [tok for tok in run_concurrently(redeem_and_issue) if tok]
        db.remove()
        live = [tok for tok in minted if AccessToken.authenticate(tok) is not None]
        assert not live, (
            f"{len(live)} token(s) survived a detected code replay")


    def test_issue_rereads_the_grant_it_locked(self, client):
        """The ordering the barrier test cannot produce.

        `run_concurrently` releases both callers at `redeem`, and in-process
        the winner always reaches `issue()` before the loser's revocation
        commits — so the loser sweeps the winner's token afterwards and the
        dangerous interleaving never happens. It passed 12/12 against a server
        leaking 28%.

        The order that matters is: winner claims, LOSER REVOKES AND COMMITS,
        winner issues. Driving it deterministically needs no threads, only a
        second connection — and it catches the failure the barrier misses,
        which is that `redeem` leaves the grant in the session's identity map,
        so a `with_for_update()` without `populate_existing()` takes the lock
        and then hands back the stale cached row.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import scoped_session, sessionmaker

        verifier, challenge = pkce()
        code = AuthorizationCode.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            redirect_uri=REDIRECT, scope="loans:read", code_challenge=challenge)

        row, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err is None

        # What routes/oauth2.py does to build issue()'s arguments. This is the
        # step that un-expires the instance, and therefore the whole bug.
        _ = (row.patron_email_hash, row.scope, row.id)

        # The loser, on its own connection: detects the replay and revokes.
        other = scoped_session(sessionmaker(bind=create_engine(
            engine.url.render_as_string(hide_password=False))))
        other.execute(text(
            "UPDATE oauth_authorization_codes SET grant_revoked_at = now() "
            "WHERE id = :i"), {"i": row.id})
        other.commit()
        other.remove()

        # The winner now issues. It must refuse.
        with pytest.raises(oauth2.GrantRevoked):
            AccessToken.issue(
                client_id=client.client_id,
                patron_email_hash=row.patron_email_hash,
                scope=row.scope, authorization_code_id=row.id)


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

    def test_refresh_reuse_detection_leaves_no_live_token(self, client):
        """The same race as the code path, and the one that matters in
        practice: a refresh token is a 90-day credential, so this is the
        collision an attacker with a stolen one actually gets to have.

        Needs a grant row, because that is what the two callers serialise on —
        a family with `authorization_code_id` NULL has nothing to lock and
        `revoke_for_code` refuses to act on it.
        """
        verifier, challenge = pkce()
        code = AuthorizationCode.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            redirect_uri=REDIRECT, scope="loans:read", code_challenge=challenge)
        row, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err is None
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)

        def rotate():
            issued, err = AccessToken.refresh(refresh, client_id=client.client_id)
            return issued[0] if issued else None

        minted = [tok for tok in run_concurrently(rotate) if tok]
        db.remove()
        live = [tok for tok in minted if AccessToken.authenticate(tok) is not None]
        assert not live, (
            f"{len(live)} token(s) survived a detected refresh replay")


class TestPerPatronLoanLimit:
    """The limit `/oauth2/borrow` is the first caller able to break.

    `Item.borrow` takes `SELECT ... FOR UPDATE` on the *item*, which is what
    stops one copy going to two patrons. It does nothing for the per-patron
    limit: borrowing N different editions takes N different item locks, so the
    calls never contend and every one of them counts the same stale set of
    active loans.

    A browser cannot exploit that — a patron gets one click at a time. A backend
    consumer holding an OAuth token issues the requests in parallel without
    trying, which is why this endpoint is what turned the race into a bypass.
    """

    def test_parallel_borrows_of_different_editions_respect_the_limit(self, monkeypatch):
        from lenny import configs
        from lenny.core.models import FormatEnum, Item, Loan

        limit = 2
        monkeypatch.setattr(configs, "get_loan_limit", lambda: limit)
        monkeypatch.setattr(configs, "get_loan_duration_days", lambda: 14)

        editions = []
        for n in range(5):
            item = Item(openlibrary_edition=90000 + n, encrypted=True,
                        formats=FormatEnum.PDF)
            db.add(item)
            editions.append(item)
        db.commit()
        item_ids = [i.id for i in editions]
        db.remove()

        def borrow(item_id):
            def go():
                item = db.query(Item).filter(Item.id == item_id).first()
                try:
                    item.borrow(PATRON, hashed=True)
                    return True
                except Exception:
                    return False
            return go

        # One thread per edition, all released together.
        barrier = threading.Barrier(len(item_ids))
        results, lock = [], threading.Lock()

        def worker(item_id):
            try:
                barrier.wait(timeout=20)
                out = borrow(item_id)()
                with lock:
                    results.append(out)
            finally:
                db.remove()

        threads = [threading.Thread(target=worker, args=(i,)) for i in item_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        db.remove()
        active = db.query(Loan).filter(
            Loan.patron_email_hash == PATRON, *Loan._active_filters()).count()
        assert active <= limit, (
            f"{active} concurrent loans for a patron limited to {limit}")
        # Without this the test is green when EVERY borrow failed — a deadlock,
        # a leaked lock, an exception storm all leave `active` at 0. The limit
        # has to be reached, not just not exceeded.
        assert active == limit, (
            f"only {active} of {limit} allowed loans succeeded; the lock is "
            "blocking legitimate borrows, not just illegitimate ones")
        assert sum(1 for r in results if r) == limit, (
            f"{sum(1 for r in results if r)} borrows reported success but "
            f"{active} loans exist")

    def test_reborrowing_the_same_item_returns_a_usable_loan(self, monkeypatch):
        """The idempotent path releases both locks before returning, and
        `db.rollback()` expires the object it hands back — so the caller's
        `loan.due_date` has to survive a refresh. Nothing else covers this
        path, which is why changing it needed a test of its own."""
        from lenny import configs
        from lenny.core.models import FormatEnum, Item, Loan

        monkeypatch.setattr(configs, "get_loan_limit", lambda: 3)
        monkeypatch.setattr(configs, "get_loan_duration_days", lambda: 14)

        item = Item(openlibrary_edition=91234, encrypted=True,
                    formats=FormatEnum.PDF)
        db.add(item)
        db.commit()

        first = item.borrow(PATRON, hashed=True)
        first_id = first.id
        again = item.borrow(PATRON, hashed=True)

        assert again.id == first_id, "a second borrow created a second loan"
        assert again.due_date is not None, (
            "the returned loan was left expired and unusable by the rollback")
        assert db.query(Loan).filter(
            Loan.patron_email_hash == PATRON,
            *Loan._active_filters()).count() == 1
