"""Ghost as a subscription provider: entitlement tokens in, member webhooks in.

A member signed in to the publisher's Ghost site gets a short-lived token from
Ghost's own `/members/api/entitlements/` (RS512, 5 min, verifiable against the
site's JWKS). The site's Code Injection snippet posts it to Lenny, which checks
it here and grants a lease. Lenny never holds a Ghost Admin API key.

Settings live in auth.env and are read fresh on every call, like the other
admin-editable settings, so every worker agrees right after an admin change.
"""
import hashlib
import hmac
import logging
import os
import secrets
import time
import urllib.parse

import httpx

from lenny import configs
from lenny.core.cache import Cache

logger = logging.getLogger(__name__)

PROVIDER = "ghost"
LEASE_SECONDS = 24 * 3600
ENTITLEMENT_SCOPE = "members:entitlements:read"
WEBHOOK_MAX_SKEW_MS = 5 * 60 * 1000
JWKS_TTL_S = 600

URL_KEY = "LENNY_GHOST_URL"
TIERS_KEY = "LENNY_GHOST_TIERS"
SECRET_KEY = "LENNY_GHOST_WEBHOOK_SECRET"


class GhostError(Exception):
    """`code` is stable and shown to admins/patrons; the message is for logs."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def _setting(key: str) -> str:
    raw = configs._read_env_value(configs.AUTH_ENV_PATH, key)
    return (raw if raw is not None else os.environ.get(key, "")).strip()


def site_url() -> str:
    return _setting(URL_KEY).rstrip("/")


def allowed_tiers() -> list:
    return [t for t in (x.strip() for x in _setting(TIERS_KEY).split(",")) if t]


def webhook_secret() -> str:
    return _setting(SECRET_KEY)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def normalize_site_url(url: str) -> str:
    """https origin (plus optional path for a Ghost mounted under one), no trailing slash."""
    url = (url or "").strip().rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or parts.username:
        raise GhostError("invalid_site_url", "Ghost site URL must be an https:// address")
    return url


# ── JWKS ────────────────────────────────────────────────────────────────────

# shortcut: per-worker cache with a fixed TTL; a key rotated inside the TTL fails for up to 10 min.
_jwks_cache: dict = {}


def fetch_jwks(site: str) -> dict:
    """Fetch and sanity-check the site's member signing keys."""
    try:
        r = httpx.get(f"{site}/members/.well-known/jwks.json", timeout=5.0, follow_redirects=False)
    except httpx.HTTPError as e:
        raise GhostError("ghost_unreachable", f"JWKS fetch failed: {e}")
    if r.status_code != 200:
        raise GhostError("ghost_no_jwks", f"JWKS answered {r.status_code}")
    try:
        jwks = r.json()
        keys = [k for k in jwks.get("keys", []) if k.get("kty") == "RSA"]
    except (ValueError, AttributeError):
        raise GhostError("ghost_no_jwks", "JWKS is not JSON")
    if not keys:
        raise GhostError("ghost_no_jwks", "JWKS has no RSA keys")
    return {"keys": keys}


def _jwks(site: str) -> dict:
    hit = _jwks_cache.get(site)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    jwks = fetch_jwks(site)
    _jwks_cache[site] = (time.monotonic() + JWKS_TTL_S, jwks)
    return jwks


# ── entitlement token ───────────────────────────────────────────────────────

def verify_entitlement(token: str) -> dict:
    """Return the token's claims if it proves a paid, allowed member; else raise GhostError."""
    from authlib.jose import JsonWebKey, JsonWebToken
    from authlib.jose.errors import JoseError

    site = site_url()
    if not site:
        raise GhostError("not_configured")
    if not token or len(token) > 8192:
        raise GhostError("invalid_token", "missing or oversized token")

    issuer = f"{site}/members/api"
    try:
        claims = JsonWebToken(["RS512"]).decode(
            token, JsonWebKey.import_key_set(_jwks(site)),
            claims_options={
                "iss": {"essential": True, "value": issuer},
                "aud": {"essential": True, "value": issuer},
                "exp": {"essential": True},
                "sub": {"essential": True},
                "jti": {"essential": True},
            })
        claims.validate(leeway=30)
    except GhostError:
        raise
    except (JoseError, ValueError) as e:
        raise GhostError("invalid_token", f"{type(e).__name__}: {e}")

    # Ghost signs identity and entitlement tokens with the same key, iss and aud;
    # only `scope` tells them apart.
    if claims.get("scope") != ENTITLEMENT_SCOPE:
        raise GhostError("invalid_token", "wrong scope")
    if claims.get("paid") is not True:
        raise GhostError("not_paid")
    allowed = allowed_tiers()
    if allowed and not set(allowed) & set(map(str, claims.get("active_tier_ids") or [])):
        raise GhostError("tier_not_allowed")
    # Single use: counted then recorded, keyed to outlive the token (5 min + leeway).
    if Cache.is_throttled("ghost:jti", str(claims["jti"])[:128], 1, 600, fail_closed=True):
        raise GhostError("token_reused")
    return dict(claims)


# ── webhooks ────────────────────────────────────────────────────────────────

def verify_webhook_signature(raw_body: bytes, header: str, secret: str, now_ms: int = None) -> bool:
    """Ghost: `X-Ghost-Signature: sha256=<hex HMAC-SHA256(secret, body + t)>, t=<ms>`."""
    if not (secret and header):
        return False
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    sig, ts = parts.get("sha256", ""), parts.get("t", "")
    if not sig or not ts.isdigit():
        return False
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if abs(now_ms - int(ts)) > WEBHOOK_MAX_SKEW_MS:
        return False
    expected = hmac.new(secret.encode(), raw_body + ts.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def member_change(payload: dict):
    """From a member.edited / member.deleted payload, decide what to revoke.

    Returns (member_uuid, [emails to revoke]) or None when access should stay.
    Never logs the payload: it carries the member's email.
    """
    member = (payload or {}).get("member") or {}
    cur, prev = member.get("current") or {}, member.get("previous") or {}
    uuid = cur.get("uuid") or prev.get("uuid")
    emails = [e for e in (cur.get("email"), prev.get("email")) if e]
    if not cur:  # member.deleted
        return uuid, emails
    if cur.get("status") == "free":
        return uuid, emails
    allowed = allowed_tiers()
    if allowed and "tiers" in cur and not set(allowed) & {str(t.get("id")) for t in cur.get("tiers") or []}:
        return uuid, emails
    if prev.get("email") and cur.get("email") and prev["email"].strip().lower() != cur["email"].strip().lower():
        # The old address no longer proves anyone; the member relaunches under the new one.
        return None, [prev["email"]]
    return None


def mark_webhook() -> None:
    Cache._record("ghost:webhook", "last", 90 * 86400, value=str(int(time.time())))


def last_webhook_at():
    from lenny.core.cache import CacheEntry
    from lenny.core.db import session as db
    import datetime
    row = (db.query(CacheEntry.value).filter(CacheEntry.scope == "ghost:webhook")
           .order_by(CacheEntry.expires_at.desc()).first())
    if not row or not (row[0] or "").isdigit():
        return None
    return datetime.datetime.fromtimestamp(int(row[0]), datetime.timezone.utc).isoformat()


# ── the snippet publishers paste into Ghost ─────────────────────────────────

SNIPPET = """<script>
/* Lenny: open members-only books. Put Read buttons in posts as
   <button data-lenny-book="BOOK_ID">Read</button> */
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-lenny-book]");
  if (!b) return;
  e.preventDefault();
  fetch(%(entitlements)s, {credentials: "same-origin"}).then(function (r) {
    if (r.status !== 200) { window.location.hash = "#/portal/signin"; return; }
    return r.text().then(function (token) {
      var f = document.createElement("form");
      f.method = "POST";
      f.action = %(launch)s;
      [["token", token], ["book", b.getAttribute("data-lenny-book")]].forEach(function (p) {
        var i = document.createElement("input");
        i.type = "hidden"; i.name = p[0]; i.value = p[1];
        f.appendChild(i);
      });
      document.body.appendChild(f);
      f.submit();
    });
  });
});
</script>"""


def snippet(launch_url: str, site: str) -> str:
    import json
    return SNIPPET % {"launch": json.dumps(launch_url),
                      "entitlements": json.dumps(f"{site}/members/api/entitlements/")}
