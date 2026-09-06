"""Unit tests for the OAuth 2.0 authorization-server primitives.

These pin the properties the design in lenny#209 claims. Each test named
`test_attack_*` asserts that a specific attack fails; if one of those ever goes
green-to-red, a real defence has been removed.

Everything here runs against the real models and a real (in-memory) database —
the point is the SQL behaviour, not a mock of it.
"""

import base64
import datetime
import hashlib
import os

import pytest
import sqlalchemy

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "oauth2-unit-test-seed-32-chars-ok")

from lenny.core import oauth2  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import (  # noqa: E402
    AccessToken,
    AuthorizationCode,
    OAuthClient,
    verify_pkce,
)

REDIRECT = "https://consumer.example.org/callback"
OTHER_REDIRECT = "https://consumer.example.org/other"
PATRON = "patron-email-hash-aaa"


@pytest.fixture(autouse=True)
def fresh_db():
    """A clean schema per test.

    These tables hold single-use artefacts, so leaking rows between tests would
    quietly make replay tests pass for the wrong reason.
    """
    Base.metadata.create_all(engine)
    yield
    db.remove()
    for table in ("oauth_access_tokens", "oauth_authorization_codes", "oauth_clients"):
        try:
            db.execute(sqlalchemy.text(f"DELETE FROM {table}"))
        except Exception:
            db.rollback()
    db.commit()
    db.remove()


def pkce():
    verifier = "a" * 64
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture
def client():
    obj, secret = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT, OTHER_REDIRECT],
        scopes=["loans:read", "borrow"],
    )
    obj._plain_secret = secret
    return obj


def issue_code(client, *, challenge, redirect=REDIRECT, scope="loans:read"):
    return AuthorizationCode.issue(
        client_id=client.client_id, patron_email_hash=PATRON,
        redirect_uri=redirect, scope=scope, code_challenge=challenge,
    )


# ─────────────────────────────────────────────────────────────────────────────
# PKCE
# ─────────────────────────────────────────────────────────────────────────────

class TestPKCE:
    def test_correct_verifier_passes(self):
        verifier, challenge = pkce()
        assert verify_pkce(verifier, challenge) is True

    def test_attack_wrong_verifier_fails(self):
        _, challenge = pkce()
        assert verify_pkce("b" * 64, challenge) is False

    def test_attack_plain_method_refused(self):
        """`plain` gives no protection against anyone who can already read the
        authorization request, which is precisely the attacker PKCE exists for."""
        verifier, _ = pkce()
        assert verify_pkce(verifier, verifier, method="plain") is False

    @pytest.mark.parametrize("verifier,challenge", [("", "x"), ("x", ""), ("", "")])
    def test_empty_inputs_fail(self, verifier, challenge):
        assert verify_pkce(verifier, challenge) is False


# ─────────────────────────────────────────────────────────────────────────────
# Client registration
# ─────────────────────────────────────────────────────────────────────────────

class TestOAuthClient:
    def test_secret_is_not_stored_in_the_clear(self, client):
        assert client.client_secret_hash != client._plain_secret
        assert len(client.client_secret_hash) == 64
        assert client.verify_secret(client._plain_secret) is True

    def test_attack_wrong_secret_refused(self, client):
        assert client.verify_secret("not-the-secret") is False
        assert client.verify_secret(None) is False

    def test_registered_redirect_allowed(self, client):
        assert client.allows_redirect(REDIRECT) is True

    def test_attack_unregistered_redirect_refused(self, client):
        assert client.allows_redirect("https://evil.example.com/callback") is False

    def test_attack_redirect_prefix_is_not_enough(self, client):
        """Exact match only. Prefix matching is the classic way an authorization
        code gets delivered somewhere it should not."""
        assert client.allows_redirect(REDIRECT + "/../evil") is False
        assert client.allows_redirect(REDIRECT + ".evil.com") is False
        assert client.allows_redirect(REDIRECT + "?next=https://evil.com") is False

    def test_omitted_scope_grants_everything_registered(self, client):
        granted, err = client.resolve_scope(None)
        assert err is None
        assert set(granted.split()) == {"borrow", "loans:read"}

    def test_attack_scope_escalation_refused(self):
        """A client registered for loans:read must not obtain borrow."""
        narrow, _ = OAuthClient.register(
            name="Loans only", redirect_uris=[REDIRECT], scopes=["loans:read"])
        granted, err = narrow.resolve_scope("borrow")
        assert granted is None
        assert "not registered" in err

    def test_unknown_scope_is_an_error_not_a_silent_drop(self, client):
        granted, err = client.resolve_scope("loans:read admin:everything")
        assert granted is None
        assert "unknown scope" in err


# ─────────────────────────────────────────────────────────────────────────────
# Authorization codes
# ─────────────────────────────────────────────────────────────────────────────

class TestAuthorizationCode:
    def test_happy_path(self, client):
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err is None
        assert row.patron_email_hash == PATRON

    def test_code_is_not_stored_in_the_clear(self, client):
        _, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        assert db.query(AuthorizationCode).filter(
            AuthorizationCode.code_hash == code).first() is None

    def test_attack_replay_refused(self, client):
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        _, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err is None
        _, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err == "code already redeemed"

    def test_attack_replay_revokes_tokens_from_that_code(self, client):
        """RFC 6749 §4.1.2: reuse means the code probably leaked, so the tokens
        it produced must die too — declining the second attempt is not enough."""
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, _ = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        access, _refresh, _tok = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)
        assert AccessToken.authenticate(access) is not None

        AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert AccessToken.authenticate(access) is None

    def test_attack_different_client_refused(self, client):
        other, _ = OAuthClient.register(name="Someone else", redirect_uris=[REDIRECT])
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        _, err = AuthorizationCode.redeem(
            code, client_id=other.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert "different client" in err

    def test_attack_different_redirect_uri_refused(self, client):
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge, redirect=REDIRECT)
        _, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=OTHER_REDIRECT,
            code_verifier=verifier)
        assert "redirect_uri does not match" in err

    def test_attack_wrong_verifier_refused(self, client):
        _, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        _, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier="b" * 64)
        assert "PKCE" in err

    def test_expired_code_refused(self, client):
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row = db.query(AuthorizationCode).first()
        row.expires_at = oauth2._now() - datetime.timedelta(seconds=1)
        db.add(row)
        db.commit()
        _, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err == "code expired"

    def test_unknown_code_refused(self, client):
        verifier, _ = pkce()
        _, err = AuthorizationCode.redeem(
            "never-issued", client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err == "invalid code"


# ─────────────────────────────────────────────────────────────────────────────
# Access and refresh tokens
# ─────────────────────────────────────────────────────────────────────────────

class TestAccessToken:
    def test_issue_and_authenticate(self, client):
        access, _, tok = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        found = AccessToken.authenticate(access)
        assert found is not None
        assert found.id == tok.id
        assert found.has_scope("loans:read") is True
        assert found.has_scope("borrow") is False

    def test_token_is_not_stored_in_the_clear(self, client):
        access, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        rows = db.query(AccessToken).all()
        assert all(r.token_hash != access for r in rows)
        assert all(r.refresh_token_hash != refresh for r in rows)

    def test_not_ip_bound(self, client):
        """Deliberate: a consumer calls from its own servers, so the address
        presenting the token is never the patron's. Session cookies are IP-bound
        (core/auth.py); these must not be."""
        assert not hasattr(AccessToken, "ip")
        access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        assert AccessToken.authenticate(access) is not None

    def test_attack_expired_token_refused(self, client):
        access, _, tok = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        tok.expires_at = oauth2._now() - datetime.timedelta(seconds=1)
        db.add(tok)
        db.commit()
        assert AccessToken.authenticate(access) is None

    def test_attack_revoked_token_refused(self, client):
        access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        assert AccessToken.revoke(access) is True
        assert AccessToken.authenticate(access) is None

    def test_revoke_by_refresh_token_kills_the_access_token(self, client):
        access, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        assert AccessToken.revoke(refresh) is True
        assert AccessToken.authenticate(access) is None

    def test_refresh_rotates(self, client):
        """A stolen refresh token stops working the moment the legitimate holder
        uses theirs."""
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        issued, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err is None
        new_access, new_refresh, _ = issued
        assert new_refresh != refresh
        assert AccessToken.authenticate(new_access) is not None

        _, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err is not None, "the old refresh token must be dead after rotation"

    def test_refresh_preserves_patron_and_scope(self, client):
        """A refresh must not silently widen or reassign what was granted."""
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        issued, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err is None
        _, _, tok = issued
        assert tok.patron_email_hash == PATRON
        assert tok.scope == "loans:read"

    def test_attack_refresh_by_different_client_refused(self, client):
        other, _ = OAuthClient.register(name="Someone else", redirect_uris=[REDIRECT])
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        _, err = AccessToken.refresh(refresh, client_id=other.client_id)
        assert "different client" in err

    def test_attack_expired_refresh_refused(self, client):
        _, refresh, tok = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        tok.refresh_expires_at = oauth2._now() - datetime.timedelta(seconds=1)
        db.add(tok)
        db.commit()
        _, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err == "refresh token expired"

    def test_revoke_is_idempotent(self, client):
        """Revoking twice must not raise or resurrect anything."""
        access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        assert AccessToken.revoke(access) is True
        assert AccessToken.revoke(access) is True
        assert AccessToken.authenticate(access) is None

    def test_revoke_reports_unknown_tokens_internally(self, client):
        """The model distinguishes "no such token" so callers can log it. The
        HTTP endpoint deliberately does not surface this — RFC 7009 treats
        telling a caller whether a token existed as a disclosure in itself."""
        assert AccessToken.revoke("never-issued") is False


# ─────────────────────────────────────────────────────────────────────────────
# Regressions from the milestone-1 adversarial review.
#
# Each of these passed a 74-test suite while the defence was absent. They are
# the failures that review found, kept as tests so they cannot come back.
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewRegressions:
    def test_attack_stolen_refresh_used_first_is_cut_off(self, client):
        """Rotation alone only punishes whoever moves second.

        An attacker who exfiltrates a refresh token and uses it *before* the
        legitimate client ends up holding the live family; the victim gets an
        error, re-authorizes, and notices nothing. Detecting reuse has to revoke
        the family (RFC 9700 §4.14.2, OAuth 2.1 §6.1), not just decline.
        """
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, _ = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)

        stolen, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err is None
        attacker_access, attacker_refresh, _ = stolen

        # The victim presents their copy; the server now knows two parties hold it.
        _, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err is not None

        assert AccessToken.authenticate(attacker_access) is None, (
            "the attacker's access token survived detected refresh reuse")
        _, err = AccessToken.refresh(attacker_refresh, client_id=client.client_id)
        assert err is not None, "the attacker could keep rotating indefinitely"

    def test_attack_foreign_client_cannot_revoke_with_a_spent_code(self, client):
        """Spent codes are not secret — they sit in browser history, Referer
        headers and consumer access logs. Replaying one as an unrelated client
        must not destroy the real client's tokens."""
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, _ = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        victim_access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)

        attacker, _ = OAuthClient.register(
            name="Throwaway", redirect_uris=["https://evil.example/cb"])
        _, err = AuthorizationCode.redeem(
            code, client_id=attacker.client_id,
            redirect_uri="https://evil.example/cb", code_verifier="z" * 64)

        assert "different client" in err, f"leaked which failure occurred: {err}"
        assert AccessToken.authenticate(victim_access) is not None, (
            "an unrelated client revoked the victim's tokens with a spent code")

    def test_revoking_one_token_kills_the_whole_grant(self, client):
        """RFC 7009 §2.1. A patron withdrawing access must actually disconnect
        the consumer, even if it has since rotated onto a newer pair."""
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, _ = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        _, refresh1, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)
        issued, _ = AccessToken.refresh(refresh1, client_id=client.client_id)
        access2, _, _ = issued

        assert AccessToken.revoke(refresh1) is True
        assert AccessToken.authenticate(access2) is None, (
            "the rotated-onto access token outlived revocation of its grant")

    def test_revoke_for_code_none_revokes_nothing(self, client):
        """`None` would compile to `IS NULL` and revoke every token with no
        grant recorded, across all patrons."""
        a1, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash="patron-A", scope="loans:read")
        a2, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash="patron-B", scope="loans:read")
        assert AccessToken.revoke_for_code(None) == 0
        db.commit()
        assert AccessToken.authenticate(a1) is not None
        assert AccessToken.authenticate(a2) is not None

    @pytest.mark.parametrize("verifier", ["é" * 64, "abcd", "a" * 42, "a" * 129, "a b" * 20])
    def test_malformed_verifiers_are_refused_not_raised(self, verifier):
        """RFC 7636 §4.1: 43-128 unreserved ASCII characters.

        The 43-character floor *is* the 256-bit entropy requirement — a shorter
        verifier is guessable from an intercepted challenge, which removes the
        only guarantee PKCE offers. A validator must also never raise: a
        non-ASCII verifier used to escape as a 500 instead of `invalid_grant`.
        """
        digest = hashlib.sha256(verifier.encode("utf-8", "ignore")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert verify_pkce(verifier, challenge) is False

    def test_tokens_carry_real_entropy(self, client):
        """A mutation shrinking tokens to 32 bits passed the entire suite."""
        access, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        for token in (access, refresh, client.client_id):
            padded = token + "=" * (-len(token) % 4)
            assert len(base64.urlsafe_b64decode(padded)) >= 16, (
                f"{token!r} carries too little entropy to resist guessing")

    def test_authorization_code_ttl_matches_the_documented_60s(self):
        """A code travels through the browser, so its life is the window an
        interception has to be useful in. Asserted exactly, because an earlier
        `<= 600` made a 10x widening invisible."""
        assert oauth2.AUTH_CODE_TTL == 60


class TestOperatorControls:
    """An operator needs to stop a client whose mandate ended or whose
    credentials leaked, and needs the tables not to grow forever."""

    def test_a_disabled_client_cannot_be_used(self, client):
        """Instant revocation without hand-written SQL, and without destroying
        the audit trail a deletion would take with it."""
        client.disabled_at = oauth2._now()
        db.add(client)
        db.commit()
        assert OAuthClient.get(client.client_id) is None

    def test_disabling_a_client_revokes_its_live_tokens(self, client):
        """Otherwise 'disabled' means 'cannot get new tokens' while the ones it
        already holds keep working for up to an hour."""
        access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        assert AccessToken.authenticate(access) is not None
        OAuthClient.disable(client.client_id)
        assert AccessToken.authenticate(access) is None

    def test_sweep_removes_only_long_expired_codes(self, client):
        """Codes are single-use and live 60 seconds; nothing ever deleted them.
        The sweep must not touch a code still inside its window."""
        _, challenge = pkce()
        fresh = issue_code(client, challenge=challenge)
        stale = issue_code(client, challenge=challenge)
        row = db.query(AuthorizationCode).filter(
            AuthorizationCode.code_hash == oauth2._hash(stale)).first()
        row.expires_at = oauth2._now() - datetime.timedelta(days=2)
        db.add(row)
        db.commit()

        assert oauth2.sweep_expired() == 1
        remaining = {r.code_hash for r in db.query(AuthorizationCode).all()}
        assert oauth2._hash(fresh) in remaining
        assert oauth2._hash(stale) not in remaining

    def test_sweep_keeps_tokens_that_are_still_refreshable(self, client):
        """An access token expires in an hour but its refresh token lives 90
        days. Deleting the row on access expiry would break every refresh."""
        access, refresh, tok = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        tok.expires_at = oauth2._now() - datetime.timedelta(days=2)
        db.add(tok)
        db.commit()

        oauth2.sweep_expired()
        issued, err = AccessToken.refresh(refresh, client_id=client.client_id)
        assert err is None, "a refreshable token was swept away"


class TestReuseDetectionActuallyRevokes:
    """Round 2, H5: a TOCTOU window after the atomic claim.

    The claim itself is correct, but the winner's token is created *after* it
    commits. A loser arriving in that window detects reuse and calls
    revoke_for_code — which sweeps nothing, because the winner's token does not
    exist yet — and the winner's token is then created live. The defence that
    `test_attack_stolen_refresh_used_first_is_cut_off` exists to provide is
    absent exactly when two requests overlap, which is the case it is for.
    """

    def test_a_token_issued_after_reuse_was_detected_is_dead(self, client):
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err is None

        # The loser lands here — after the winner claimed, before it issued.
        _, err = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        assert err == "code already redeemed"

        # The winner now issues. Its token must not be live: reuse of this code
        # has already been observed.
        access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)
        assert AccessToken.authenticate(access) is None, (
            "a token was issued live for a grant whose code was already "
            "known to have been replayed")

    def test_a_refresh_issued_after_family_revocation_is_dead(self, client):
        verifier, challenge = pkce()
        code = issue_code(client, challenge=challenge)
        row, _ = AuthorizationCode.redeem(
            code, client_id=client.client_id, redirect_uri=REDIRECT,
            code_verifier=verifier)
        _, refresh, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)

        # Winner claims the rotation; loser detects reuse and revokes the family.
        AccessToken.refresh(refresh, client_id=client.client_id)
        AccessToken.refresh(refresh, client_id=client.client_id)

        # Anything issued for this grant afterwards must also be dead.
        late, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON,
            scope="loans:read", authorization_code_id=row.id)
        assert AccessToken.authenticate(late) is None, (
            "a token issued after the grant was revoked is live")


class TestOpenLibraryToggle:
    """Open Library is the consumer nearly every node wants, so connecting it is
    one command rather than an incantation. Being safe to run twice matters:
    an operator who is unsure whether it is already connected should be able to
    just run it."""

    @pytest.fixture(autouse=True)
    def node_is_signed_in(self, monkeypatch):
        """These tests are about the toggle, not the handoff to Open Library.

        Give the node the credentials `make ol-login` would have set, and stub
        the outbound registration — otherwise every case here would depend on
        Open Library being reachable from the test runner.
        """
        from lenny import configs
        from scripts import oauth2_client
        monkeypatch.setattr(configs, "OL_S3_ACCESS_KEY", "node-access")
        monkeypatch.setattr(configs, "OL_S3_SECRET_KEY", "node-secret")
        monkeypatch.setattr(oauth2_client, "_register_with_openlibrary",
                            lambda *a: (True, "registered with Open Library (stub)"))

    def _connect(self, **kw):
        import argparse
        from scripts.oauth2_client import OPENLIBRARY_REDIRECT, cmd_ol_connect
        args = argparse.Namespace(redirect_uri=OPENLIBRARY_REDIRECT, rotate=False)
        for k, v in kw.items():
            setattr(args, k, v)
        return cmd_ol_connect(args)

    def _disconnect(self):
        import argparse
        from scripts.oauth2_client import cmd_ol_disconnect
        return cmd_ol_disconnect(argparse.Namespace())

    def test_connecting_twice_does_not_duplicate(self):
        assert self._connect() == 0
        assert self._connect() == 0
        from scripts.oauth2_client import OPENLIBRARY_NAME
        assert db.query(OAuthClient).filter(
            OAuthClient.name == OPENLIBRARY_NAME).count() == 1

    def test_disconnect_then_reconnect_restores_the_same_client(self):
        """The secret was only ever shown once, so reconnecting must not
        silently invalidate it — an operator would have no way to recover."""
        self._connect()
        from scripts.oauth2_client import _openlibrary_client
        before = _openlibrary_client().client_id
        assert self._disconnect() == 0
        assert _openlibrary_client().disabled_at is not None
        assert self._connect() == 0
        after = _openlibrary_client()
        assert after.client_id == before
        assert after.disabled_at is None

    def test_disconnect_revokes_live_tokens(self):
        """Disconnecting has to stop access now, not in an hour when the access
        token happens to expire."""
        self._connect()
        from scripts.oauth2_client import _openlibrary_client
        client = _openlibrary_client()
        access, _, _ = AccessToken.issue(
            client_id=client.client_id, patron_email_hash=PATRON, scope="loans:read")
        assert AccessToken.authenticate(access) is not None
        self._disconnect()
        assert AccessToken.authenticate(access) is None

    def test_rotate_retires_the_old_registration_and_its_tokens(self):
        """Rotating away from a leaked secret is pointless if the tokens it
        already produced keep working."""
        self._connect()
        from scripts.oauth2_client import _openlibrary_client
        old = _openlibrary_client()
        access, _, _ = AccessToken.issue(
            client_id=old.client_id, patron_email_hash=PATRON, scope="loans:read")

        assert self._connect(rotate=True) == 0
        new = _openlibrary_client()
        assert new.client_id != old.client_id, "rotate must issue a new client"
        assert AccessToken.authenticate(access) is None, (
            "tokens from the retired registration are still live")

    def test_disconnecting_when_never_connected_is_not_an_error(self):
        assert self._disconnect() == 0

    def test_connect_requires_the_node_to_have_its_own_credentials(self, monkeypatch):
        """Open Library has to know *which* Lenny is calling, and the node's own
        S3 keys are that identity. Without them there is nothing to register
        with, so say what to do rather than printing a secret to carry by hand.
        """
        import argparse

        from lenny import configs
        from scripts.oauth2_client import OPENLIBRARY_REDIRECT, cmd_ol_connect
        monkeypatch.setattr(configs, "OL_S3_ACCESS_KEY", None)
        monkeypatch.setattr(configs, "OL_S3_SECRET_KEY", None)
        args = argparse.Namespace(redirect_uri=OPENLIBRARY_REDIRECT, rotate=False)
        assert cmd_ol_connect(args) == 1, "connecting without node credentials must fail"

    def test_a_failed_handoff_still_shows_the_secret(self, monkeypatch):
        """If Open Library cannot be reached the node is still configured, so the
        operator needs the secret to finish by hand — losing it would mean
        rotating for no reason."""
        import argparse

        from lenny import configs
        from scripts import oauth2_client
        monkeypatch.setattr(configs, "OL_S3_ACCESS_KEY", "access")
        monkeypatch.setattr(configs, "OL_S3_SECRET_KEY", "secret")
        monkeypatch.setattr(oauth2_client, "_register_with_openlibrary",
                            lambda *a: (False, "Open Library unreachable"))
        args = argparse.Namespace(
            redirect_uri=oauth2_client.OPENLIBRARY_REDIRECT, rotate=False)
        assert oauth2_client.cmd_ol_connect(args) == 0
