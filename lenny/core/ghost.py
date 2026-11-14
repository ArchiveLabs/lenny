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
import re
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
ENABLED_KEY = "LENNY_GHOST_ENABLED"


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


def enabled() -> bool:
    """Admin on/off switch. Off keeps the site, tiers, secret and passes; it only stops reading."""
    return _setting(ENABLED_KEY).lower() != "false"


def webhook_secret() -> str:
    return _setting(SECRET_KEY)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# Plain https host, optional port and path. Nothing else: the URL is written into the
# snippet publishers paste into their site, so quotes, spaces or `<` must never get through.
_SITE_URL = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%/-]*)?")


def normalize_site_url(url: str) -> str:
    """https origin (plus optional path for a Ghost mounted under one), no trailing slash."""
    url = (url or "").strip().rstrip("/")
    try:
        ok = bool(_SITE_URL.fullmatch(url)) and (urllib.parse.urlsplit(url).port or 443) <= 65535
    except ValueError:
        ok = False
    if not ok:
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


# ── tier names ──────────────────────────────────────────────────────────────

# Ghost's Portal script carries the site's public, read-only Content API key on every page.
_CONTENT_KEY = re.compile(r'data-key="([0-9a-f]{26})"')
TIERS_TTL_S = 3600
# shortcut: per-worker cache, failures cached too; a renamed tier shows its old name for up to an hour.
_tiers_cache: dict = {}


def tier_names(site: str) -> dict:
    """{paid tier id: name} from the site's public Content API, or {} when it can't be read.

    Display only, so redirects are followed (a custom domain sends the API to *.ghost.io)
    and only tier names are kept from the answer.
    """
    hit = _tiers_cache.get(site)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    names = {}
    try:
        with httpx.Client(timeout=5.0, follow_redirects=True) as client:
            key = _CONTENT_KEY.search(client.get(f"{site}/").text)
            if key:
                r = client.get(f"{site}/ghost/api/content/tiers/", params={"key": key.group(1), "limit": "all"})
                # Free tiers can't open a book (launch needs `paid`), so offering them only misleads.
                names = {t["id"]: t["name"] for t in r.json().get("tiers", [])
                         if isinstance(t.get("id"), str) and isinstance(t.get("name"), str)
                         and t.get("type") != "free"}
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as e:
        logger.info("Ghost tier names unavailable for %s: %s", site, e)
    _tiers_cache[site] = (time.monotonic() + TIERS_TTL_S, names)
    return names


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
    tiers = cur.get("tiers")
    if allowed and isinstance(tiers, list) and not set(allowed) & {str(t.get("id")) for t in tiers if isinstance(t, dict)}:
        return uuid, emails
    if prev.get("email") and cur.get("email") and prev["email"].strip().lower() != cur["email"].strip().lower():
        # The old address no longer proves anyone; the member relaunches under the new one.
        return None, [prev["email"]]
    return None


REVOKED_SCOPE = "ghost:revoked"
REVOKED_TTL_S = 15 * 60  # outlives any token Ghost issued before the revocation (5 min + leeway)


def mark_revoked(keys) -> None:
    """Remember when access ended for these member ids / email hashes."""
    now = str(int(time.time()))
    for key in filter(None, keys):
        Cache._record(REVOKED_SCOPE, key, REVOKED_TTL_S, value=now)


def revoked_after_issue(keys, issued_at) -> bool:
    """True if any key was revoked at or after the token's `iat` (or the token has no `iat`)."""
    from lenny.core.cache import CacheEntry
    from lenny.core.db import session as db
    import datetime
    keys = [k for k in keys if k]
    if not keys:
        return False
    rows = db.query(CacheEntry.value).filter(
        CacheEntry.scope == REVOKED_SCOPE, CacheEntry.key.in_(keys),
        CacheEntry.expires_at > datetime.datetime.now(datetime.timezone.utc)).all()
    stamps = [int(v) for (v,) in rows if (v or "").isdigit()]
    if not stamps:
        return False
    return not isinstance(issued_at, int) or max(stamps) >= issued_at


def _stamp(scope: str) -> None:
    """Remember "now" for this scope: one row, replaced each time."""
    from lenny.core.cache import CacheEntry
    from lenny.core.db import session as db
    db.query(CacheEntry).filter(CacheEntry.scope == scope).delete(synchronize_session=False)
    Cache._record(scope, "last", 365 * 86400, value=str(int(time.time())))  # commits both


def _stamped(scope: str):
    from lenny.core.cache import CacheEntry
    from lenny.core.db import session as db
    import datetime
    row = (db.query(CacheEntry.value).filter(CacheEntry.scope == scope)
           .order_by(CacheEntry.expires_at.desc()).first())
    if not row or not (row[0] or "").isdigit():
        return None
    return datetime.datetime.fromtimestamp(int(row[0]), datetime.timezone.utc).isoformat()


def mark_webhook() -> None:
    _stamp("ghost:webhook")


def last_webhook_at():
    return _stamped("ghost:webhook")


def mark_launch() -> None:
    """A Read button worked. Setup proof only, so a failure here never blocks the reader."""
    try:
        _stamp("ghost:launch")
    except Exception:
        logger.warning("Could not record the last Ghost launch", exc_info=True)


def last_launch_at():
    return _stamped("ghost:launch")


# shortcut: per-worker cache; another worker may still say "not installed" for up to a minute after a paste.
_installed_cache: dict = {}


def snippet_installed(site: str, marker: str):
    """True if the site's home page carries this exact snippet, False if not, None if unreachable.

    A found snippet is remembered for 10 minutes and an unreachable site for one;
    "not found" is never cached, so a fresh paste shows up on the next look.
    """
    hit = _installed_cache.get((site, marker))
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        r = httpx.get(f"{site}/", timeout=5.0, follow_redirects=True)
        found = (marker in r.text) if r.status_code == 200 else None
    except httpx.HTTPError:
        found = None
    if found is not False:
        _installed_cache[(site, marker)] = (time.monotonic() + (600 if found else 60), found)
    return found


# ── the snippet publishers paste into Ghost ─────────────────────────────────

SNIPPET = """<script>
/* Lenny: open members-only books. Paste into Ghost: Settings > Code injection > Site header.
   In a post, link to this site's address + #lenny-book=BOOK_ID, as either
   a Button card, or an HTML card: <a href="https://YOUR-SITE/#lenny-book=BOOK_ID">Read</a>
   Links work on the site and in newsletters (the link opens the site, which opens the book). */
(function () {
  var root = document.documentElement;
  function show() { root.style.visibility = ""; }
  function open(book, askToSignIn) {
    fetch(%(entitlements)s, {credentials: "same-origin"}).then(function (r) {
      if (r.status !== 200) {
        show();
        if (!askToSignIn) return;
        try { localStorage.setItem("lennyBook", JSON.stringify([book, Date.now()])); } catch (e) {}
        window.location.hash = "#/portal/signin";
        return;
      }
      return r.text().then(function (token) {
        function go() {
          var f = document.createElement("form");
          f.method = "POST";
          f.action = %(launch)s;
          [["token", token], ["book", book]].forEach(function (p) {
            var i = document.createElement("input");
            i.type = "hidden"; i.name = p[0]; i.value = p[1];
            f.appendChild(i);
          });
          document.body.appendChild(f);
          f.submit();
        }
        if (document.body) go(); else document.addEventListener("DOMContentLoaded", go);
      });
    }).catch(show);
  }
  // Coming back with the browser's Back button must not find the page still hidden.
  window.addEventListener("pageshow", function (e) { if (e.persisted) show(); });
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest('[data-lenny-book], a[href*="#lenny-book="]');
    if (!b) return;
    e.preventDefault();
    open(b.getAttribute("data-lenny-book") ||
         decodeURIComponent(b.getAttribute("href").split("#lenny-book=")[1] || ""), true);
  });
  var m = location.hash.match(/^#lenny-book=(.+)$/), book = null, saved = null;
  if (m) {  // arrived from a newsletter or another page's link
    try { book = decodeURIComponent(m[1]); } catch (e) {}
    history.replaceState(null, "", location.pathname + location.search);
  } else {
    try { saved = JSON.parse(localStorage.getItem("lennyBook")); localStorage.removeItem("lennyBook"); } catch (e) {}
    // Back from Ghost's sign-in link within 30 minutes: open the book they asked for.
    if (saved && /[?&]success=true/.test(location.search) && Date.now() - saved[1] < 1800000) book = saved[0];
  }
  if (!book) return;
  root.style.visibility = "hidden";  // arriving to open a book: don't flash the page first
  open(book, !!m);
})();
</script>"""


def _snippet_body(launch_url: str, site: str) -> str:
    import json

    def js(s):  # a JSON string that can't close the <script> it sits in
        return json.dumps(s).replace("</", "<\\/")
    return SNIPPET % {"launch": js(launch_url), "entitlements": js(f"{site}/members/api/entitlements/")}


def snippet_marker(launch_url: str, site: str) -> str:
    """Changes whenever the snippet or Lenny's address does, so an old paste is told apart."""
    return "lenny-snippet " + hashlib.sha256(_snippet_body(launch_url, site).encode()).hexdigest()[:12]


def snippet(launch_url: str, site: str) -> str:
    return _snippet_body(launch_url, site).replace(
        "<script>", f"<script>\n/* {snippet_marker(launch_url, site)} */", 1)
