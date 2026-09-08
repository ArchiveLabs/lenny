#!/usr/bin/env python3
"""
A stand-in for Open Library, to exercise Lenny's OAuth 2.0 authorization server.

This is what Open Library would have to do. It is deliberately small — the point
is to show how little the consumer side is once Lenny speaks standard OAuth:
discover, redirect, exchange, call.

Credentials come from the node's operator (`make oauth2-register`), which is
the trust decision a library makes rather than something a consumer helps
itself to.

    python tests/oauth2/mock_openlibrary.py --lenny http://127.0.0.1:8080

Run it against a Lenny with a patron session available. It plays every role OL
would: a confidential client with its own credentials, a redirect endpoint that
receives the authorization code, and a backend that exchanges the code and calls
the resource API.

What it is NOT: a browser. Where a real patron would be redirected to Lenny and
click "Allow", this script drives those two steps with an HTTP client carrying
the patron's session cookie, and says so as it goes.
"""

import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

REDIRECT_URI = None
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8092/callback"


def _c(text, colour):
    return f"\033[{colour}m{text}\033[0m"


def step(n, title):
    print(f"\n{_c(f'── {n} ' + '─' * (68 - len(title) - len(str(n))) + ' ' + title, '36')}")


def ok(msg):
    print(f"   {_c('✓', '32')} {msg}")


def info(msg):
    print(f"     {_c(msg, '90')}")


def fail(msg):
    print(f"   {_c('✗', '31')} {msg}")
    sys.exit(1)


def _fresh_token(client, base, meta, basic, cookies, params) -> str:
    """Walk consent and exchange once more, returning a live access token."""
    verifier, challenge = pkce_pair()
    page = client.get(f"{meta['authorization_endpoint']}?" + urlencode(
        {**params, "state": secrets.token_urlsafe(8), "code_challenge": challenge}),
        cookies=cookies)
    r = client.post(f"{base}/v1/api/oauth2/authorize", cookies=cookies,
                    data={"request": _handle_from(page.text), "decision": "allow"})
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = client.post(meta["token_endpoint"], headers={"Authorization": f"Basic {basic}"},
                    data={"grant_type": "authorization_code", "code": code,
                          "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    if r.status_code != 200:
        fail(f"could not mint a fresh token: {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


def _json(r, what: str):
    """A response body, or a readable failure.

    A proxy in front of the node answers with HTML — a 429 or a 502 — and
    calling .json() on that raises a JSONDecodeError that hides what actually
    happened.
    """
    try:
        return r.json()
    except ValueError:
        fail(f"{what}: HTTP {r.status_code}, non-JSON body "
             f"(a proxy in front of the node?): {r.text[:160]}")


def _handle_from(html: str) -> str:
    """Pull the consent form's opaque handle out of the rendered page.

    A real patron's browser does this by submitting the form. The handle is
    signed and bound to that patron, so a consumer cannot mint one itself —
    which is the point.
    """
    m = re.search(r'name="request" value="([^"]+)"', html)
    if not m:
        fail("consent page has no request handle")
    return m.group(1)


def pkce_pair() -> tuple[str, str]:
    """RFC 7636. The verifier stays on this server; only its hash is sent
    through the browser, so intercepting the redirect yields nothing usable."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lenny", default="http://127.0.0.1:8080",
                    help="base URL of the Lenny node")
    ap.add_argument("--session", default=None,
                    help="a patron's Lenny session cookie (stands in for the browser)")
    ap.add_argument("--edition", type=int, default=None,
                    help="edition id to borrow via the API")
    ap.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI,
                    help="this consumer's callback URL, as registered")
    ap.add_argument("--client-id", help="from `make oauth2-register`")
    ap.add_argument("--client-secret", help="from `make oauth2-register`")
    ap.add_argument("--narrow-client-id",
                    help="a second client registered with only loans:read, to "
                         "show scope enforcement")
    ap.add_argument("--harvest", metavar="PATH",
                    help="a harvest produced by Open Library's own OPDS parser "
                         "(see the module docstring); borrows a book it found "
                         "rather than one named with --edition")
    args = ap.parse_args()

    global REDIRECT_URI
    REDIRECT_URI = args.redirect_uri

    base = args.lenny.rstrip("/")

    # With --harvest, the edition comes from what Open Library actually parsed
    # out of the live feed, which makes this the whole OL->Lenny arc rather than
    # a Lenny-only demo.
    if args.harvest:
        harvest = json.load(open(args.harvest))
        acq = harvest["chosen"]["acquisitions"][0]
        args.edition = int(acq["local_id"])
        print(_c(f"\nOpen Library harvested {harvest['borrowable']} borrowable "
                 f"publications from {harvest['publications']} in the live feed.", "90"))
        print(_c(f"Borrowing {harvest['chosen']['title'][:50]!r} "
                 f"(edition {args.edition}) — availability "
                 f"{acq['data'].get('availability')}.", "90"))
    client = httpx.Client(timeout=30.0, follow_redirects=False)

    print(_c(f"\nMock Open Library → Lenny node at {base}", "1"))

    # ── 1. Discovery (RFC 8414) ──────────────────────────────────────────────
    # OL knows only the node's base URL — from feed_registry.url. Everything
    # else is discovered, which is what makes N nodes tractable.
    step(1, "Discover the node (RFC 8414)")
    r = client.get(f"{base}/.well-known/oauth-authorization-server")
    if r.status_code != 200:
        fail(f"no metadata document (HTTP {r.status_code})")
    meta = r.json()
    ok("found /.well-known/oauth-authorization-server")
    for k in ("authorization_endpoint", "token_endpoint", "revocation_endpoint"):
        info(f"{k}: {meta[k]}")
    info(f"scopes: {' '.join(meta['scopes_supported'])}")
    if "S256" not in meta.get("code_challenge_methods_supported", []):
        fail("node does not advertise PKCE S256")
    ok("PKCE S256 supported")

    # ── 2. Credentials the operator issued ───────────────────────────────────
    # Not self-registration: a Lenny operator decides who may act on their
    # patrons' behalf, and hands out credentials with `make oauth2-register`.
    step(2, "Use the credentials this node's operator issued")
    if not args.client_id:
        fail("no --client-id. On the Lenny node run:\n"
             '       lenny oauth2-register NAME="Open Library" '
             f'URI={REDIRECT_URI}\n'
             "     then pass the printed --client-id and --client-secret here.")
    client_id, client_secret = args.client_id, args.client_secret
    ok(f"acting as {client_id}")
    info("no self-registration: the node's operator chose to trust this client")

    # ── 3. Authorization request ─────────────────────────────────────────────
    step(3, "Send the patron to authorize")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "loans:read borrow",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{meta['authorization_endpoint']}?{urlencode(params)}"
    ok("built the authorization URL the patron's browser would follow")
    info(auth_url[:110] + "…")
    info(f"code_verifier kept server-side ({verifier[:12]}…), never sent to the browser")

    if not args.session:
        print(f"\n{_c('Stopping here: no --session given.', '33')}")
        print("Open the URL above in a browser logged in to Lenny, click Allow,")
        print("then re-run with --session <your lenny session cookie>.")
        return

    # Standing in for the browser: same request, carrying the patron's session.
    cookies = {"session": args.session}
    r = client.get(auth_url, cookies=cookies)
    if r.status_code == 303 and "/oauth/authorize" in r.headers.get("location", ""):
        fail("Lenny did not recognise the session cookie — it redirected to login. "
             "Check the cookie value (and that its bound IP matches this host).")
    if r.status_code != 200:
        fail(f"authorize returned {r.status_code}: {r.text[:300]}")
    ok("patron is asked to consent (scopes shown on Lenny's own page)")
    handle = _handle_from(r.text)
    info("the form carries one opaque handle, not the request parameters —")
    info("so this consumer cannot alter what the patron approved")

    # ── 4. Consent ───────────────────────────────────────────────────────────
    step(4, "Patron clicks Allow")
    r = client.post(f"{base}/v1/api/oauth2/authorize", cookies=cookies,
                    data={"request": handle, "decision": "allow"})
    if r.status_code != 303:
        fail(f"expected a redirect back to the client, got {r.status_code}: {r.text[:300]}")
    location = r.headers["location"]
    returned = parse_qs(urlparse(location).query)
    if returned.get("state", [None])[0] != state:
        fail("state mismatch — this is the CSRF check, and it failed")
    ok("redirected back to the client with a code")
    code = returned["code"][0]
    info(f"state verified: {state[:10]}…")
    info(f"code: {code[:12]}… (single use, 60s)")

    # ── 5. Token exchange — the back channel ─────────────────────────────────
    # This is why a code in a URL is safe: redeeming it needs the client secret
    # and the verifier, and the browser has never seen either.
    step(5, "Exchange the code for tokens (back channel)")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = client.post(meta["token_endpoint"],
                    headers={"Authorization": f"Basic {basic}"},
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": REDIRECT_URI,
                        "code_verifier": verifier,
                    })
    if r.status_code != 200:
        fail(f"token exchange failed: {r.status_code} {r.text[:300]}")
    tok = _json(r, "token exchange")
    access, refresh = tok["access_token"], tok["refresh_token"]
    ok(f"got an access token (expires in {tok['expires_in']}s) and a refresh token")
    info(f"scope: {tok['scope']}")
    info("OL stores these per patron; the patron never sees them")

    # ── 6. The code is single-use ────────────────────────────────────────────
    step(6, "Replaying the code must fail")
    r = client.post(meta["token_endpoint"],
                    headers={"Authorization": f"Basic {basic}"},
                    data={"grant_type": "authorization_code", "code": code,
                          "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    if r.status_code == 200:
        fail("the code was accepted twice — single-use enforcement is broken")
    ok(f"replay refused: {r.json().get('error_description')}")
    info("and every token descended from that code is now revoked")

    # A revoked token must stop working immediately.
    probe = client.get(f"{base}/v1/api/oauth2/loans",
                       headers={"Authorization": f"Bearer {access}"})
    if probe.status_code != 401:
        fail(f"token still live after code reuse (HTTP {probe.status_code}) — "
             "revocation-on-reuse is not working")
    ok("the previously issued token was revoked too, as RFC 6749 §4.1.2 requires")

    # Re-authorize to continue the demonstration with a live token.
    step(7, "Re-authorize to continue")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    auth_url2 = f"{meta['authorization_endpoint']}?" + urlencode({
        **params, "state": state, "code_challenge": challenge})
    r = client.get(auth_url2, cookies=cookies)
    r = client.post(f"{base}/v1/api/oauth2/authorize", cookies=cookies,
                    data={"request": _handle_from(r.text), "decision": "allow"})
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    r = client.post(meta["token_endpoint"],
                    headers={"Authorization": f"Basic {basic}"},
                    data={"grant_type": "authorization_code", "code": code,
                          "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
    tok = _json(r, "token exchange")
    access, refresh = tok["access_token"], tok["refresh_token"]
    ok("fresh token issued")

    # ── 8. Call the resource API ─────────────────────────────────────────────
    step(8, "Read the patron's loans (scope loans:read)")
    r = client.get(f"{base}/v1/api/oauth2/loans",
                   headers={"Authorization": f"Bearer {access}"})
    if r.status_code != 200:
        fail(f"loans call failed: {r.status_code} {r.text[:300]}")
    loans = r.json()["loans"]
    ok(f"{len(loans)} active loan(s)")
    for loan in loans:
        info(f"edition {loan['edition_id']}  due {loan['due_at'] or 'no expiry'}")
    info("no S3 keys were sent, and this token can only see THIS patron")

    # ── 9. Borrow ────────────────────────────────────────────────────────────
    if args.edition:
        step(9, f"Borrow edition {args.edition} (scope borrow)")
        r = client.post(f"{base}/v1/api/oauth2/borrow",
                        headers={"Authorization": f"Bearer {access}"},
                        data={"edition_id": args.edition})
        if r.status_code not in (200, 201):
            fail(f"borrow failed: {r.status_code} {r.text[:300]}")
        body = r.json()
        ok(f"{body['status']}: edition {body['edition_id']}, due {body.get('due_at') or 'no expiry'}")

        r = client.get(f"{base}/v1/api/oauth2/loans",
                       headers={"Authorization": f"Bearer {access}"})
        ok(f"loans now: {len(r.json()['loans'])}")

    # ── 10. Refresh ──────────────────────────────────────────────────────────
    step(10, "Refresh the access token")
    r = client.post(meta["token_endpoint"],
                    headers={"Authorization": f"Basic {basic}"},
                    data={"grant_type": "refresh_token", "refresh_token": refresh})
    if r.status_code != 200:
        fail(f"refresh failed: {r.status_code} {r.text[:300]}")
    tok2 = r.json()
    ok("new access token issued without involving the patron")
    info("this is why the patron authorizes once, not once per session")

    r = client.post(meta["token_endpoint"],
                    headers={"Authorization": f"Basic {basic}"},
                    data={"grant_type": "refresh_token", "refresh_token": refresh})
    if r.status_code == 200:
        fail("the old refresh token still works — rotation is not happening")
    ok(f"the old refresh token is dead: {r.json().get('error_description')}")

    # ── 11. Scope enforcement ────────────────────────────────────────────────
    step(11, "Scopes are enforced, not advisory")
    if args.narrow_client_id:
        r = client.get(f"{base}/v1/api/oauth2/authorize", cookies=cookies, params={
            "client_id": args.narrow_client_id, "redirect_uri": REDIRECT_URI,
            "response_type": "code", "scope": "borrow", "state": "x",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        if "error=invalid_scope" not in r.headers.get("location", ""):
            fail("a client asked for a scope it never registered and was not refused")
        ok("a client cannot request a scope it did not register for")
    else:
        info("skipped: pass --narrow-client-id for a client registered with only")
        info("loans:read to exercise this. Covered by test_oauth2_endpoints.py.")

    # ── 12. Revocation ───────────────────────────────────────────────────────
    step(12, "Revoke (RFC 7009)")
    # Re-authorize first: step 10 rotated a refresh token twice, and reuse
    # detection already revoked that whole family. Revoking a token that is
    # dead anyway proves nothing — which is exactly what this step used to do.
    fresh = _fresh_token(client, base, meta, basic, cookies, params)
    probe = client.get(f"{base}/v1/api/oauth2/loans",
                       headers={"Authorization": f"Bearer {fresh}"})
    if probe.status_code != 200:
        fail(f"the token to revoke was not live to begin with: {probe.status_code}")

    r = client.post(f"{base}/v1/api/oauth2/revoke",
                    headers={"Authorization": f"Basic {basic}"},
                    data={"token": fresh})
    if r.status_code != 200:
        fail(f"revocation itself failed: HTTP {r.status_code} {r.text[:200]}")
    r = client.get(f"{base}/v1/api/oauth2/loans",
                   headers={"Authorization": f"Bearer {fresh}"})
    if r.status_code != 401:
        fail(f"revoked token still works (HTTP {r.status_code})")
    ok("a live token, revoked with client credentials, is rejected immediately")

    print(f"\n{_c('All steps passed.', '32;1')}")
    print(_c("No IA S3 keys crossed the boundary. No bulk key exists. Every token "
             "is one patron's, granted by that patron.", "90"))


if __name__ == "__main__":
    main()
