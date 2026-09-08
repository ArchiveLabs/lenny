#!/usr/bin/env python

"""
OAuth 2.0 authorization server for Lenny.

Lenny is the authorization server *and* the resource server. A consumer — Open
Library, another catalogue, a reading app — is a client acting on a patron's
behalf. The patron is the resource owner.

Why this exists (see ArchiveLabs/lenny#209): Lenny stores "logged in" as a
cookie on its own domain, so a consumer cannot POST to Lenny and receive a
usable session — `Set-Cookie` lands on the consumer's HTTP client, not the
patron's browser. Every workaround (forwarding IA S3 keys, a service key that
reads any patron's loans, the consumer signing identity assertions) either moves
credentials somewhere they don't belong or creates a key whose leak exposes
every patron. Authorization Code + PKCE avoids all of it: every token is
per-patron, granted by that patron, and the token itself never travels through
the browser.

Grants supported: `authorization_code` (PKCE required) and `refresh_token`.
The implicit grant is deliberately absent — OAuth 2.1 removes it, and the
token-in-URL exposure is exactly what this replaces.

Secrets at rest: authorization codes, access tokens, refresh tokens and client
secrets are all stored as SHA-256 hashes. A dump of these tables yields nothing
replayable.
"""

import base64
import datetime
import hashlib
import re
import secrets
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    or_,
)
from sqlalchemy import Integer
from sqlalchemy.sql import func

from lenny.core.db import Base
from lenny.core.db import session as db

# ─────────────────────────────────────────────────────────────────────────────
# Lifetimes
#
# The authorization code is the only artefact that travels through the browser,
# so it is the shortest-lived and single-use. RFC 6749 §4.1.2 recommends a
# maximum of 10 minutes; 60 seconds is enough for a redirect and leaves far less
# room for a code captured from an access log or a referrer header.
# ─────────────────────────────────────────────────────────────────────────────
AUTH_CODE_TTL = 60                    # seconds
ACCESS_TOKEN_TTL = 60 * 60            # 1 hour
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 90  # 90 days
# An absolute ceiling on one consent, anchored to the authorization code rather
# than to the newest token. Rotation renews the 90 days above on every use, so
# without this a single "Allow" click grants access for as long as the consumer
# keeps refreshing — indefinitely. The consent screen tells the patron their
# access expires; this is what makes that true.
GRANT_MAX_TTL = 60 * 60 * 24 * 365     # 1 year

# Scopes this server understands. Anything else is rejected at /authorize
# rather than silently narrowed, so a client learns immediately.
SCOPES = {
    "loans:read": "See which books you have on loan",
    "borrow": "Borrow and return books on your behalf",
}


_PK = BigInteger().with_variant(Integer, "sqlite")


class GrantRevoked(Exception):
    """A token was requested against a grant that reuse killed. See
    `AccessToken.issue`; callers turn this into `invalid_grant`."""


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _as_utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Coerce a stored timestamp to timezone-aware UTC.

    Postgres `timestamptz` round-trips as aware; SQLite drops the tzinfo and
    hands back a naive value. Comparing the two raises, so every expiry check
    goes through here rather than assuming a backend.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=datetime.timezone.utc)


def _hash(value: str) -> str:
    """SHA-256 of a token. Tokens are high-entropy random strings, so a plain
    digest is appropriate — unlike passwords, there is nothing to brute-force."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mint(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


# urlparse().hostname strips the brackets from an IPv6 literal, so "::1"
# is the form actually compared.
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# A reverse-DNS scheme: at least one dot, and only characters a URI scheme may
# contain. `com.example.reader` qualifies; `myapp` and `javascript` do not.
_REVERSE_DNS_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*\.[a-z0-9+.\-]+$")


def acceptable_redirect(uri: str) -> bool:
    """Whether a redirect_uri may be registered.

    Enforced by `OAuthClient.register`, so it is an invariant of the stored
    record rather than something each caller has to remember.

    `https://` anywhere, plus `http://` on the loopback interface. The loopback
    exemption is RFC 8252 §7.3: the authorization code never crosses a network,
    and without it a developer cannot run a client against a local node.

    The host is compared against the *parsed* hostname, so `127.0.0.1.evil.com`
    and `http://127.0.0.1@evil.com/` are refused — a substring check here would
    hand codes to whoever registered the lookalike.
    """
    try:
        parsed = urlparse(uri)
    except ValueError:
        return False
    # RFC 6749 §3.1.2 — a fragment would put the authorization code after the
    # '#', where it never reaches the client's server. Control characters are
    # simply not URI syntax. A bare trailing '#' parses to an empty fragment,
    # which is falsy, so it needs testing on the raw string.
    if "#" in uri:
        return False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in uri):
        return False
    if parsed.scheme == "https" and parsed.netloc:
        return True

    # RFC 8252 §7.1 — a native app redirects to a private-use URI scheme,
    # because it has no https origin to own. Lenny already hands native OPDS
    # readers `opds://authorize/` through the older flow, so refusing these
    # would lock out exactly the clients this system exists to serve.
    #
    # The scheme must be one the client plausibly controls: reverse-DNS, per the
    # RFC, or `opds`, the OPDS convention this deployment already speaks. A
    # single-label scheme like `myapp://` is squattable by any other app on the
    # device, which is the attack §7.1 is written against.
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        return parsed.scheme == "opds" or _REVERSE_DNS_SCHEME.match(parsed.scheme) is not None

    if parsed.scheme != "http":
        return False
    # `username@host` puts the real host after the '@'; urlparse.hostname
    # already resolves that, which is why this must not look at netloc.
    return (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


# ─────────────────────────────────────────────────────────────────────────────
# Client registration
# ─────────────────────────────────────────────────────────────────────────────

class OAuthClient(Base):
    """A registered consumer.

    `redirect_uris` is a newline-separated allowlist, compared by exact string
    match. Exact matching is deliberate: prefix or wildcard matching on redirect
    URIs is the classic way authorization codes get delivered to an attacker.
    """

    __tablename__ = "oauth_clients"

    id = Column(_PK, primary_key=True)
    client_id = Column(String(64), nullable=False, unique=True, index=True)
    # NULL for a public client (PKCE-only, no secret to keep).
    client_secret_hash = Column(String(64), nullable=True)
    name = Column(String(255), nullable=False)
    redirect_uris = Column(Text, nullable=False)
    scopes = Column(Text, nullable=False, default="")
    is_confidential = Column(Boolean, nullable=False, default=True)
    # Disable a client without deleting it — a consumer's mandate ends, or its
    # credentials leak. Deleting the row would take the audit trail with it,
    # and is blocked anyway by the codes that reference it.
    disabled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    # ── lookup ───────────────────────────────────────────────────────────────

    @classmethod
    def get(cls, client_id: str) -> Optional["OAuthClient"]:
        """The live client with this id, or None.

        A disabled client is invisible here, so every path that looks one up —
        authorize, token, revoke — refuses it without needing its own check.
        """
        if not client_id:
            return None
        return db.query(cls).filter(
            cls.client_id == client_id,
            cls.disabled_at == None,  # noqa: E711
        ).first()

    # ── registration ─────────────────────────────────────────────────────────

    @classmethod
    def register(cls, name: str, redirect_uris: list[str],
                 scopes: Optional[list[str]] = None,
                 is_confidential: bool = True) -> tuple["OAuthClient", Optional[str]]:
        """Create a client. Returns `(client, client_secret)`.

        The secret is returned exactly once and never recoverable afterwards —
        only its hash is stored, so a database dump yields nothing replayable.

        Raises ValueError for a redirect_uri that could not be honoured.
        """
        for uri in redirect_uris:
            if not acceptable_redirect(uri):
                raise ValueError(
                    f"{uri!r} cannot be a redirect_uri: it must be an absolute "
                    "https:// URL, http:// on loopback, or a private-use scheme "
                    "such as opds:// or com.example.app:// (RFC 8252).")

        secret = _mint(32) if is_confidential else None
        client = cls(
            client_id=_mint(16),
            client_secret_hash=_hash(secret) if secret else None,
            name=name,
            redirect_uris="\n".join(redirect_uris),
            scopes=" ".join(scopes or list(SCOPES)),
            is_confidential=is_confidential,
        )
        db.add(client)
        db.commit()
        return client, secret

    @classmethod
    def disable(cls, client_id: str) -> int:
        """Stop a client and revoke what it already holds.

        Blocking new tokens is not enough on its own: the tokens a client is
        already carrying stay valid for up to an hour, and its refresh tokens
        for ninety days. Returns the number of tokens revoked.
        """
        row = db.query(cls).filter(cls.client_id == client_id).first()
        if row is None:
            return 0
        row.disabled_at = _now()
        db.add(row)
        tokens = db.query(AccessToken).filter(
            AccessToken.client_id == client_id,
            AccessToken.revoked_at == None,  # noqa: E711
        ).all()
        for token in tokens:
            token.revoked_at = _now()
            db.add(token)
        db.commit()
        return len(tokens)

    # ── checks ───────────────────────────────────────────────────────────────

    def verify_secret(self, secret: Optional[str]) -> bool:
        """Check a client secret. A public client has none and always passes —
        PKCE is what authenticates it (RFC 8252)."""
        if not self.is_confidential:
            return True
        if not secret or not self.client_secret_hash:
            return False
        return secrets.compare_digest(_hash(secret), self.client_secret_hash)

    def allows_redirect(self, redirect_uri: str) -> bool:
        """Exact match against the registered list. No prefix or wildcard."""
        if not redirect_uri:
            return False
        return redirect_uri in [u.strip() for u in self.redirect_uris.splitlines() if u.strip()]

    def allowed_scopes(self) -> set[str]:
        return {s for s in (self.scopes or "").split() if s}

    def resolve_scope(self, requested: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """Resolve the scope for a request.

        Returns `(granted_scope, error)`. An unknown or unregistered scope is an
        error rather than a silent narrowing, so a client is told plainly
        instead of discovering a missing permission at call time.
        """
        allowed = self.allowed_scopes()
        if not requested:
            return " ".join(sorted(allowed)), None
        asked = {s for s in requested.split() if s}
        if unknown := asked - set(SCOPES):
            return None, f"unknown scope(s): {' '.join(sorted(unknown))}"
        if ungranted := asked - allowed:
            return None, f"scope(s) not registered for this client: {' '.join(sorted(ungranted))}"
        return " ".join(sorted(asked)), None


# ─────────────────────────────────────────────────────────────────────────────
# Authorization codes
# ─────────────────────────────────────────────────────────────────────────────

class AuthorizationCode(Base):
    """A single-use, short-lived code bound to one client, redirect and PKCE
    challenge.

    The binding is what makes the code safe to send through a browser: a code
    intercepted in transit is useless without the `code_verifier`, which never
    leaves the client, and cannot be redeemed by a different client or
    redirected somewhere else.
    """

    __tablename__ = "oauth_authorization_codes"
    __table_args__ = (Index("idx_oauth_codes_expires", "expires_at"),)

    id = Column(_PK, primary_key=True)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    client_id = Column(String(64), ForeignKey("oauth_clients.client_id"), nullable=False)
    patron_email_hash = Column(String, nullable=False)
    redirect_uri = Column(Text, nullable=False)
    scope = Column(Text, nullable=False, default="")
    code_challenge = Column(String(128), nullable=False)
    code_challenge_method = Column(String(10), nullable=False, default="S256")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    redeemed_at = Column(DateTime(timezone=True), nullable=True)
    # Set when this grant is found to have been replayed. Recorded on the grant
    # rather than only on its tokens so that ordering stops mattering: a token
    # issued *after* the replay was detected is still born revoked.
    grant_revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    @classmethod
    def issue(cls, *, client_id: str, patron_email_hash: str, redirect_uri: str,
              scope: str, code_challenge: str,
              code_challenge_method: str = "S256") -> str:
        """Mint a code. Only its hash is stored; the caller gets the one copy."""
        code = _mint(32)
        db.add(cls(
            code_hash=_hash(code),
            client_id=client_id,
            patron_email_hash=patron_email_hash,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=_now() + datetime.timedelta(seconds=AUTH_CODE_TTL),
        ))
        db.commit()
        return code

    @classmethod
    def redeem(cls, code: str, *, client_id: str, redirect_uri: str,
               code_verifier: str) -> tuple[Optional["AuthorizationCode"], Optional[str]]:
        """Consume a code. Returns `(row, error)`.

        Marks the row redeemed before returning it, so a concurrent second
        redemption of the same code finds it already spent.
        """
        row = db.query(cls).filter(cls.code_hash == _hash(code)).first()
        if row is None:
            return None, "invalid code"

        # Establish that the caller could plausibly be the legitimate client
        # *before* treating a spent code as evidence of a leak. Revoking first
        # would let anyone who registers a throwaway client destroy a victim's
        # tokens using a spent code — and spent codes are not secret: they sit
        # in browser history, Referer headers and the consumer's access logs.
        if row.client_id != client_id:
            return None, "code was issued to a different client"
        if row.redirect_uri != redirect_uri:
            return None, "redirect_uri does not match the authorization request"
        if not verify_pkce(code_verifier, row.code_challenge, row.code_challenge_method):
            return None, "PKCE verification failed"
        if _as_utc(row.expires_at) <= _now():
            return None, "code expired"

        # Claim the code atomically. Check-then-write is not enough: under READ
        # COMMITTED two concurrent redemptions both read `redeemed_at IS NULL`,
        # both pass every check above, and both write — so an attacker who
        # intercepts a code and races the legitimate client gets a live token
        # pair, and reuse detection never fires because neither caller saw the
        # row as spent. The UPDATE ... WHERE redeemed_at IS NULL lets the
        # database decide, and exactly one caller sees a rowcount of 1.
        claimed = db.query(cls).filter(
            cls.id == row.id, cls.redeemed_at.is_(None),
        ).update({"redeemed_at": _now()}, synchronize_session=False)
        db.commit()

        if not claimed:
            # RFC 6749 §4.1.2: a code presented twice must be refused. Reuse
            # suggests the code leaked, so revoke everything it produced rather
            # than merely declining this attempt.
            AccessToken.revoke_for_code(row.id)
            return None, "code already redeemed"

        return row, None


# RFC 7636 §4.1: 43-128 characters of unreserved ASCII. The 43-character floor
# *is* the 256-bit entropy requirement — a shorter verifier is guessable from an
# intercepted challenge, which removes the only guarantee PKCE offers. Enforcing
# the grammar also keeps a non-ASCII verifier from raising out of a validator.
PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


def verify_pkce(code_verifier: str, challenge: str, method: str = "S256") -> bool:
    """RFC 7636. Only S256 is accepted — `plain` offers no protection against an
    attacker who can already see the authorization request."""
    if method != "S256":
        return False
    if not challenge or not PKCE_VERIFIER.match(code_verifier or ""):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


# ─────────────────────────────────────────────────────────────────────────────
# Access and refresh tokens
# ─────────────────────────────────────────────────────────────────────────────

class AccessToken(Base):
    """An issued access token, with its refresh token alongside.

    Deliberately *not* IP-bound. A consumer calls Lenny from its own servers, so
    the address presenting the token is never the patron's — binding it to an IP
    the way session cookies are bound (`core/auth.py:143`) would reject every
    legitimate request.
    """

    __tablename__ = "oauth_access_tokens"
    __table_args__ = (
        Index("idx_oauth_tokens_patron", "patron_email_hash"),
        Index("idx_oauth_tokens_expires", "expires_at"),
    )

    id = Column(_PK, primary_key=True)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    refresh_token_hash = Column(String(64), nullable=True, unique=True, index=True)
    client_id = Column(String(64), ForeignKey("oauth_clients.client_id"), nullable=False)
    patron_email_hash = Column(String, nullable=False)
    scope = Column(Text, nullable=False, default="")
    # Which authorization code produced this, so code reuse can revoke its issue.
    authorization_code_id = Column(BigInteger, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    refresh_expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())

    @classmethod
    def issue(cls, *, client_id: str, patron_email_hash: str, scope: str,
              authorization_code_id: Optional[int] = None,
              ) -> tuple[str, str, "AccessToken"]:
        """Mint an access/refresh pair. Returns `(access, refresh, row)`.

        Both are returned in the clear exactly once; only their hashes persist.

        Raises `GrantRevoked` if the grant was replayed, rather than minting a
        token that is dead on arrival: a caller handed a 200 and an unusable
        token cannot tell "refreshed" from "your grant was killed for reuse",
        so it silently loses service and nothing alarms.
        """
        grant = None
        if authorization_code_id is not None:
            # SELECT ... FOR UPDATE, not a plain read. `revoke_for_code` updates
            # this same row, so taking the lock here serialises the two and the
            # interleaving stops deciding the outcome. Without the lock the
            # winner of an atomic claim reads a snapshot taken before a
            # concurrent loser's revocation committed, and inserts a *live*
            # token against a grant the server has already recorded as revoked —
            # which hands roughly half of all detected replays a working token
            # for the full access-token lifetime. This lock and the INSERT below
            # must stay in one transaction; do not commit between them.
            # populate_existing() is not optional. `redeem` already loaded
            # this row, and the route reads its attributes to build our
            # arguments, so it is live in the session's identity map. Without
            # this, SQLAlchemy takes the lock and then hands back the cached
            # instance, discarding the columns it just locked and read -- so
            # the check below sees the PRE-revocation value and the lock
            # accomplishes nothing. `Item.borrow` guards the same way
            # (models.py) for the same reason.
            grant = db.query(AuthorizationCode).filter(
                AuthorizationCode.id == authorization_code_id
            ).with_for_update().populate_existing().first()
            if grant is not None and grant.grant_revoked_at is not None:
                db.rollback()        # release the lock; we are issuing nothing
                raise GrantRevoked("This grant was revoked after a replay was detected.")

        # The ceiling is recomputed from the grant on every rotation rather
        # than stored and carried: `sweep_expired` keeps a code for as long as
        # any token references it, and nothing else deletes one, so the row is
        # always here to read. Storing it would need a column, a migration and
        # a carry-through, to hold a value we can derive in one line.
        #
        # A grant whose code was swept by the OLD sweep gets no ceiling and
        # rotates as before. That is bounded to rows that already exist and
        # cannot recur.
        ceiling = None
        if grant is not None:
            ceiling = (_as_utc(grant.created_at or _now())
                       + datetime.timedelta(seconds=GRANT_MAX_TTL))

        # Neither token may outlive the consent it descends from. Capping the
        # access token matters as much as the refresh token: without it a pair
        # minted seconds before the ceiling stays usable for the next hour,
        # which is not what "expires after a year" means to a patron.
        access_expiry = _now() + datetime.timedelta(seconds=ACCESS_TOKEN_TTL)
        refresh_expiry = _now() + datetime.timedelta(seconds=REFRESH_TOKEN_TTL)
        if ceiling is not None:
            access_expiry = min(access_expiry, ceiling)
            refresh_expiry = min(refresh_expiry, ceiling)

        access, refresh = _mint(32), _mint(32)
        row = cls(
            token_hash=_hash(access),
            refresh_token_hash=_hash(refresh),
            client_id=client_id,
            patron_email_hash=patron_email_hash,
            scope=scope,
            authorization_code_id=authorization_code_id,
            expires_at=access_expiry,
            refresh_expires_at=refresh_expiry,
        )
        db.add(row)
        db.commit()
        return access, refresh, row

    @classmethod
    def authenticate(cls, token: str) -> Optional["AccessToken"]:
        """Return the live token row for a bearer token, or None."""
        if not token:
            return None
        row = db.query(cls).filter(cls.token_hash == _hash(token)).first()
        if row is None or row.revoked_at is not None:
            return None
        if _as_utc(row.expires_at) <= _now():
            return None
        return row

    @classmethod
    def refresh(cls, refresh_token: str, *, client_id: str
                ) -> tuple[Optional[tuple[str, str, "AccessToken"]], Optional[str]]:
        """Exchange a refresh token for a new pair, rotating the refresh token.

        Rotation means a stolen refresh token stops working as soon as the
        legitimate holder uses theirs.
        """
        row = db.query(cls).filter(cls.refresh_token_hash == _hash(refresh_token)).first()
        if row is None:
            return None, "invalid refresh token"
        if row.client_id != client_id:
            return None, "refresh token was issued to a different client"

        if row.revoked_at is not None:
            # Reuse of a rotated refresh token means two parties hold it, and
            # there is no way to tell which one is the thief. RFC 9700 §4.14.2
            # and OAuth 2.1 §6.1: revoke the whole family. Without this, an
            # attacker who refreshes *first* keeps a working token for the full
            # 90-day refresh lifetime while the legitimate client quietly
            # re-authorizes and nobody notices.
            cls.revoke_for_code(row.authorization_code_id)
            return None, "refresh token revoked"

        if row.refresh_expires_at is None or _as_utc(row.refresh_expires_at) <= _now():
            return None, "refresh token expired"

        # Claim the rotation atomically, for the same reason codes are claimed
        # atomically: a check-then-write lets two parallel requests mint two
        # independent token families from one refresh token, which defeats the
        # entire point of rotating.
        claimed = db.query(cls).filter(
            cls.id == row.id, cls.revoked_at.is_(None),
        ).update({"revoked_at": _now()}, synchronize_session=False)
        db.commit()
        if not claimed:
            cls.revoke_for_code(row.authorization_code_id)
            return None, "refresh token revoked"

        try:
            issued = cls.issue(
                client_id=row.client_id,
                patron_email_hash=row.patron_email_hash,
                scope=row.scope,
                authorization_code_id=row.authorization_code_id,
            )
        except GrantRevoked:
            # A concurrent caller detected a replay of this family between our
            # claim and our issue. Say so rather than returning a dead token.
            return None, "refresh token revoked"
        return issued, None

    @classmethod
    def revoke_for_code(cls, authorization_code_id: Optional[int]) -> int:
        """Revoke every token descended from one authorization code.

        Also marks the grant itself revoked. Revoking only the rows that exist
        right now leaves a race: the winner of an atomic claim issues its token
        *after* the claim commits, so a loser detecting reuse in that window
        sweeps nothing and the winner's token is created live. Marking the grant
        makes `issue` refuse later, so the order the two requests interleave in
        no longer decides the outcome.

        A `None` id would compile to `authorization_code_id IS NULL` and revoke
        every token that has no grant recorded — across all patrons. Callers
        reach here on reuse-detection paths where the id may legitimately be
        absent, so refuse rather than trusting each of them to check.
        """
        if authorization_code_id is None:
            return 0
        db.query(AuthorizationCode).filter(
            AuthorizationCode.id == authorization_code_id,
            AuthorizationCode.grant_revoked_at == None,  # noqa: E711
        ).update({"grant_revoked_at": _now()}, synchronize_session=False)
        rows = db.query(cls).filter(
            cls.authorization_code_id == authorization_code_id,
            cls.revoked_at == None,  # noqa: E711
        ).all()
        for row in rows:
            row.revoked_at = _now()
            db.add(row)
        db.commit()
        return len(rows)

    @classmethod
    def revoke(cls, token: str, client_id: Optional[str] = None) -> bool:
        """Revoke a token and the rest of its grant. Idempotent, per RFC 7009.

        RFC 7009 §2.1 asks that revoking a refresh token also invalidate the
        access tokens issued under the same grant. Revoking only the row handed
        in would leave a patron who withdrew access still connected, because the
        consumer had since rotated onto a newer pair.

        `client_id` scopes the revocation to that client's own tokens (§5). It
        is optional so an operator can revoke from a console, but the HTTP
        endpoint always supplies it.
        """
        h = _hash(token)
        query = db.query(cls).filter(
            or_(cls.token_hash == h, cls.refresh_token_hash == h)
        )
        if client_id is not None:
            query = query.filter(cls.client_id == client_id)
        row = query.first()
        if row is None:
            return False
        if row.authorization_code_id is not None:
            cls.revoke_for_code(row.authorization_code_id)
        if row.revoked_at is None:
            row.revoked_at = _now()
            db.add(row)
            db.commit()
        return True

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scope or "").split()

    @property
    def expires_in(self) -> int:
        return max(0, int((_as_utc(self.expires_at) - _now()).total_seconds()))


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance
# ─────────────────────────────────────────────────────────────────────────────

def sweep_expired(older_than_days: int = 1) -> int:
    """Delete authorization codes and tokens that can no longer be used.

    Nothing calls this automatically — run it from a cron (`make oauth2-sweep`).
    Codes live 60 seconds and tokens an hour, but the rows outlive both.

    A token row is only removed once its refresh token is dead too — an access
    token expires in an hour while its refresh token lives ninety days, so
    sweeping on access expiry would break every legitimate refresh.

    A code outlives its own expiry, and the order below is what enforces it:
    tokens go first, then only those codes nothing descends from any more.

    The reason is the refresh path, not the code path. `refresh` calls
    `revoke_for_code` on reuse, which needs the code row present to set
    `grant_revoked_at`; and `AccessToken.issue` locks that same row to
    serialise against it. With the row gone, `issue` finds no grant, checks
    nothing, and mints a live token -- measured at 9 out of 10 refresh
    replays. Since a refresh token lives ninety days, a one-day cutoff left
    that open for the other eighty-nine.

    Code replay does not come into it: `redeem` returns "code expired" before
    it ever reaches the reuse claim, so that tripwire only exists for the
    code's 60-second life and no sweep cutoff was ever near it.

    Returns the number of rows deleted.
    """
    cutoff = _now() - datetime.timedelta(days=older_than_days)
    deleted = db.query(AccessToken).filter(
        AccessToken.refresh_expires_at != None,  # noqa: E711
        AccessToken.refresh_expires_at < cutoff,
    ).delete(synchronize_session=False)
    still_referenced = db.query(AccessToken.authorization_code_id).filter(
        AccessToken.authorization_code_id != None,  # noqa: E711
    )
    deleted += db.query(AuthorizationCode).filter(
        AuthorizationCode.expires_at < cutoff,
        AuthorizationCode.id.notin_(still_referenced),
    ).delete(synchronize_session=False)
    db.commit()
    return deleted
