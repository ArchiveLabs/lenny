#!/usr/bin/env python

"""
OAuth 2.0 authorization-server endpoints.

    GET  /oauth2/authorize   — authorization request (code + PKCE)
    POST /oauth2/authorize   — patron consent
    POST /oauth2/token       — code exchange and refresh
    POST /oauth2/revoke      — RFC 7009 revocation
    GET  /oauth2/loans       — protected resource, scope `loans:read`
    POST /oauth2/borrow      — protected resource, scope `borrow`

and, mounted at the site root by `lenny.app`:

    GET  /.well-known/oauth-authorization-server   — RFC 8414 metadata

Clients are registered by the operator (`make oauth2-register`), not through a
public endpoint: a library decides who may act on its patrons' behalf, and the
consumers here are a curated set rather than a long tail. RFC 7591 dynamic
registration is therefore deliberately absent, and the metadata does not
advertise a registration endpoint.

These live beside the existing `/oauth/*` OPDS routes rather than replacing
them. The OPDS implicit flow is what native OPDS readers speak today; this is
what a server-side consumer such as Open Library needs. See lenny#209.
"""

import base64
import logging
import re
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from lenny.core import auth
from lenny.core.exceptions import (
    BookUnavailableError,
    LoanNotRequiredError,
    PatronLoanLimitError,
)
from lenny.core.models import Item, Loan
from lenny.core.oauth2 import (
    SCOPES,
    AccessToken,
    AuthorizationCode,
    OAuthClient,
)
from lenny.core.utils import hash_email

logger = logging.getLogger(__name__)

router = APIRouter()

# How long a patron has to decide, once the consent screen is rendered.
_CONSENT_TTL = 600


# Consent ids that have been acted on. A handle is signed and short-lived, so
# this only has to outlive `_CONSENT_TTL`; entries older than that are pruned on
# write. Process-local, which is the one wrinkle — see `_spend_consent`.
_consent_spent_at: dict[str, float] = {}


def _spend_consent(consent_id: Optional[str]) -> bool:
    """Claim a consent id. True the first time, False on any replay.

    Kept in memory rather than the database because the handle is already
    signed and expires in ten minutes, so this is a replay window, not a
    security boundary. The cost is that it is per-worker: with several uvicorn
    workers a replay can land on a different worker and be honoured. That is a
    real gap and the fix is a table — noted on #209 rather than hidden here.
    """
    if not consent_id:
        return False
    now = time.time()
    for old, seen_at in list(_consent_spent_at.items()):
        if now - seen_at > _CONSENT_TTL:
            del _consent_spent_at[old]
    if consent_id in _consent_spent_at:
        return False
    _consent_spent_at[consent_id] = now
    return True


def issuer_url(request: Request) -> str:
    """This node's OAuth issuer identifier.

    Always the deployment's own configured public URL — the same
    `LennyAPI.make_url` that builds every absolute link in the OPDS feed and the
    Authentication Document. If it were wrong, those would already be wrong and
    an operator would have noticed; there is no reason for the OAuth metadata to
    invent a second source of truth.

    Specifically NOT derived from the request. RFC 8414 §3.3 makes the issuer
    security-relevant — a consumer compares it against the URL it fetched and
    refuses on mismatch — so taking it from the Host header would let anyone who
    can set that header, or seed a path-keyed cache, advertise an
    attacker-controlled token endpoint for a lax client to POST its
    client_secret and authorization code to.

    A mismatch between the configured URL and how the node was actually reached
    means the metadata advertises endpoints nobody can use, so say so loudly.
    Silence is how the forwarded-IP default survived from #201 to #210.
    """
    from lenny.core.api import LennyAPI

    issuer = LennyAPI.make_url("").rstrip("/")
    reached = str(request.base_url).rstrip("/")
    # Compare hostnames, not whole URLs: nginx forwards `Host $host`, which
    # drops the port, so a correctly configured node on a non-default port
    # would otherwise warn on every metadata fetch.
    if urlparse(issuer).hostname != urlparse(reached).hostname:
        logger.warning(
            "OAuth metadata advertises %r but this node was reached at %r. A "
            "consumer following RFC 8414 will refuse the mismatch. Set "
            "LENNY_PROXY (or LENNY_HOST/LENNY_PORT) to this node's public URL.",
            issuer, reached)
    return issuer


def _consent_serializer() -> URLSafeTimedSerializer:
    """Signs the pending authorization shown on the consent screen.

    Same primitive as the OIDC state cookie in `routes/oauth.py`; a distinct
    salt keeps the two from being interchangeable.
    """
    from lenny import configs
    return URLSafeTimedSerializer(configs.SEED, salt="oauth2-consent")


# ─────────────────────────────────────────────────────────────────────────────
# Errors
#
# RFC 6749 §4.1.2.1 draws a line that matters: if the client or redirect_uri
# cannot be validated, the error must be shown to the *patron*, never redirected
# — otherwise the endpoint becomes an open redirector that an attacker can point
# anywhere. Only once the redirect target is known to be registered may errors
# travel back to the client.
# ─────────────────────────────────────────────────────────────────────────────

def _redirect_url(redirect_uri: str, **params: str) -> str:
    """Merge params into a client's redirect_uri.

    A registered redirect_uri may already carry a query string, so the joiner
    has to be chosen rather than assumed.
    """
    joiner = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{joiner}{urlencode(params)}"


def _redirect_to(redirect_uri: str, **params: str) -> RedirectResponse:
    return RedirectResponse(url=_redirect_url(redirect_uri, **params), status_code=303)


def _error(code: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": code, "error_description": description})


def _invalid_client() -> JSONResponse:
    """RFC 6749 §5.2: a 401 for a client that attempted Basic auth MUST carry
    the challenge, or the client cannot tell what to do differently."""
    return JSONResponse(
        status_code=401,
        content={"error": "invalid_client",
                 "error_description": "Client authentication failed."},
        headers={"WWW-Authenticate": 'Basic realm="lenny"'})


def _redirect_error(redirect_uri: str, code: str, description: str,
                    state: Optional[str]) -> RedirectResponse:
    params = {"error": code, "error_description": description}
    if state:
        params["state"] = state
    return _redirect_to(redirect_uri, **params)


def _authenticated_patron(request: Request) -> Optional[str]:
    """The patron's email from their Lenny session cookie, or None.

    This is the resource owner. The authorization endpoint is the one place the
    patron's own session is the right credential — everywhere else a consumer
    presents a bearer token instead.
    """
    session = request.cookies.get("session")
    if not session:
        return None
    client_ip = request.client.host if request.client else None
    data = auth.verify_session_cookie(session, client_ip=client_ip)
    return data.get("email") if isinstance(data, dict) else None


# ─────────────────────────────────────────────────────────────────────────────
# Authorization endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/oauth2/authorize")
async def authorize(
    request: Request,
    client_id: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    response_type: Optional[str] = None,
    scope: Optional[str] = None,
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: str = "S256",
) -> Response:
    """Begin an authorization request.

    Sends the patron to log in if they have no Lenny session, then asks them to
    approve the client's requested scopes.
    """
    client = OAuthClient.get(client_id or "")
    if client is None:
        return _error("invalid_client", "Unknown client_id.")
    if not client.allows_redirect(redirect_uri or ""):
        # Not redirected back — see the note above.
        return _error("invalid_request",
                      "redirect_uri is not registered for this client.")

    # From here the redirect target is trusted, so errors may travel to it.
    if response_type != "code":
        return _redirect_error(redirect_uri, "unsupported_response_type",
                               "Only response_type=code is supported.", state)
    if not code_challenge:
        return _redirect_error(redirect_uri, "invalid_request",
                               "PKCE is required: supply code_challenge.", state)
    if code_challenge_method != "S256":
        return _redirect_error(redirect_uri, "invalid_request",
                               "code_challenge_method must be S256.", state)

    granted_scope, scope_error = client.resolve_scope(scope)
    if scope_error:
        return _redirect_error(redirect_uri, "invalid_scope", scope_error, state)

    email = _authenticated_patron(request)
    if not email:
        # No Lenny session yet. Send them through the existing OTP login and
        # come back here afterwards with the request intact.
        this_request = f"/v1/api/oauth2/authorize?{urlencode(_echo(request))}"
        return RedirectResponse(
            url=f"/v1/api/oauth/authorize?{urlencode({'redirect_uri': this_request})}",
            status_code=303,
        )

    # The form carries one opaque, signed handle instead of the request's
    # parameters. Two things follow: the POST cannot be fed a different
    # client_id or scope than the patron was shown, and it needs no second copy
    # of the validation above.
    # A random id makes the handle single-use: it is recorded when redeemed, so
    # a replay finds it spent. Without it one consent click authorised an
    # unbounded number of grants for the handle's whole lifetime, and clicking
    # "Not now" invalidated nothing.
    handle = _consent_serializer().dumps({
        "j": secrets.token_urlsafe(16),
        "c": client.client_id,
        "r": redirect_uri,
        "s": granted_scope,
        "st": state or "",
        "cc": code_challenge,
        "m": code_challenge_method,
        "p": hash_email(email),
    })

    response = request.app.templates.TemplateResponse("oauth2_consent.html", {
        "request": request,
        "client_name": client.name,
        "scopes": [(s, SCOPES[s]) for s in granted_scope.split()],
        # The operator vetted this client, so the name is trustworthy. The
        # destination is shown anyway: it is what reveals a registration made
        # in error, which is the failure mode that remains once self-
        # registration is gone.
        "redirect_host": urlparse(redirect_uri).netloc,
        "request_handle": handle,
        "email": email,
    })
    # RFC 6749 §10.13 / RFC 9700 §4.16 — this is the screen where a patron
    # grants access, so it must not be framable. The app-wide CORS policy
    # reflects any origin with credentials, which on a cookie-authenticated page
    # rendering a consent handle would let another site read it; nothing should
    # ever read this page with JavaScript, so say so explicitly.
    # Cross-origin reads are blocked by middleware in lenny.app, which also
    # covers the POST and the error paths. These two are page-specific.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return response


def _echo(request: Request) -> dict:
    """The authorization request's own parameters, for round-tripping through
    login. Rebuilt from the parsed query so nothing extra is carried along."""
    keep = ("client_id", "redirect_uri", "response_type", "scope", "state",
            "code_challenge", "code_challenge_method")
    return {k: v for k, v in request.query_params.items() if k in keep}


@router.post("/oauth2/authorize")
async def authorize_decision(
    request: Request,
    request_handle: str = Form(..., alias="request"),
    decision: str = Form("deny"),
) -> Response:
    """Record the patron's decision and redirect back to the client.

    Everything about the request comes from the signed handle minted at GET
    time, so this cannot be fed different parameters than the patron approved.
    """
    try:
        payload = _consent_serializer().loads(request_handle, max_age=_CONSENT_TTL)
    except BadSignature:
        return _error("invalid_request",
                      "This approval is no longer valid. Start the sign-in again.")

    email = _authenticated_patron(request)
    if not email:
        return _error("access_denied", "Your session expired. Start again.", status=401)

    # The handle is bound to the patron it was shown to. An attacker can mint a
    # valid handle by starting their own authorization; without this check they
    # could get a victim's browser to submit it and silently obtain a code
    # against the victim's account.
    if payload.get("p") != hash_email(email):
        return _error("access_denied",
                      "This approval was issued for a different account.", status=403)

    redirect_uri = payload["r"]
    state = payload.get("st") or None

    # Spend the handle before acting on it, whichever way the patron decided.
    # A denial has to consume it too, or replaying the same handle with
    # decision=allow would quietly overturn the refusal.
    if not _spend_consent(payload.get("j")):
        return _error("invalid_request",
                      "This approval has already been used. Start the sign-in again.")

    if decision != "allow":
        return _redirect_error(redirect_uri, "access_denied",
                               "The patron declined this request.", state)

    code = AuthorizationCode.issue(
        client_id=payload["c"],
        patron_email_hash=payload["p"],
        redirect_uri=redirect_uri,
        scope=payload["s"],
        code_challenge=payload["cc"],
        code_challenge_method=payload["m"],
    )
    # `iss` lets a client that talks to several authorization servers tell which
    # one answered (RFC 9207) — the mix-up defence. This design assumes many
    # independent nodes, so it is exactly the situation the RFC is written for.
    params = {"code": code, "iss": issuer_url(request)}
    if state:
        params["state"] = state
    target = _redirect_url(redirect_uri, **params)

    # A browser will not reliably 303 into a private-use scheme, and some refuse
    # outright. Hand the native app its link on a page instead, the way the
    # existing OPDS flow does.
    if urlparse(redirect_uri).scheme not in ("http", "https"):
        client = OAuthClient.get(payload["c"])
        return request.app.templates.TemplateResponse("oauth2_handoff.html", {
            "request": request, "target": target,
            "client_name": client.name if client else "the application",
        })
    return RedirectResponse(url=target, status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Token endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/oauth2/token")
async def token(
    request: Request,
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
) -> Response:
    """Exchange an authorization code, or refresh an access token.

    This is the back channel. It is what makes an authorization code safe to
    send through a browser: the code alone is worthless without the client's
    credentials and the PKCE verifier, neither of which the browser ever sees.
    """
    # HTTP Basic is the RFC-preferred way to present client credentials; form
    # fields are the permitted alternative. Accept both.
    basic_id, basic_secret = _basic_auth(request)
    client_id = basic_id or client_id
    client_secret = basic_secret or client_secret

    client = OAuthClient.get(client_id or "")
    if client is None or not client.verify_secret(client_secret):
        return _invalid_client()

    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return _error("invalid_request",
                          "code, redirect_uri and code_verifier are all required.")
        row, err = AuthorizationCode.redeem(
            code, client_id=client.client_id,
            redirect_uri=redirect_uri, code_verifier=code_verifier,
        )
        if err:
            logger.warning("Authorization code rejected for client %r: %s",
                           client.client_id, err)
            return _error("invalid_grant", err)
        access, refresh, tok = AccessToken.issue(
            client_id=client.client_id,
            patron_email_hash=row.patron_email_hash,
            scope=row.scope,
            authorization_code_id=row.id,
        )

    elif grant_type == "refresh_token":
        if not refresh_token:
            return _error("invalid_request", "refresh_token is required.")
        issued, err = AccessToken.refresh(refresh_token, client_id=client.client_id)
        if err:
            return _error("invalid_grant", err)
        access, refresh, tok = issued

    else:
        # Implicit is absent by design, not by omission — OAuth 2.1 removes it.
        return _error("unsupported_grant_type",
                      "Supported grants: authorization_code, refresh_token.")

    return JSONResponse({
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": tok.expires_in,
        "refresh_token": refresh,
        "scope": tok.scope,
    }, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _basic_auth(request: Request) -> tuple[Optional[str], Optional[str]]:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("basic "):
        return None, None
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except Exception:
        return None, None
    if ":" not in raw:
        return None, None
    cid, secret = raw.split(":", 1)
    return cid or None, secret or None


@router.post("/oauth2/revoke")
async def revoke(request: Request, token: str = Form(...),
                 client_id: Optional[str] = Form(None),
                 client_secret: Optional[str] = Form(None)) -> Response:
    """RFC 7009. Always 200, even for an unknown token — telling a caller
    whether a token existed is itself a disclosure.

    The client must authenticate (§2.1) and may only revoke its own tokens
    (§5). Without the ownership check, any registered client that came to hold
    another's token — forwarded to a shared downstream service, say — could
    disconnect it.
    """
    basic_id, basic_secret = _basic_auth(request)
    client = OAuthClient.get(basic_id or client_id or "")
    if client is None or not client.verify_secret(basic_secret or client_secret):
        return _invalid_client()

    AccessToken.revoke(token, client_id=client.client_id)
    return Response(status_code=200)


# ─────────────────────────────────────────────────────────────────────────────
# Protected resources
# ─────────────────────────────────────────────────────────────────────────────

def _bearer(request: Request, scope: str) -> tuple[Optional[AccessToken], Optional[Response]]:
    """Authenticate a bearer token and check one scope.

    Returns `(token, None)` or `(None, error_response)`. RFC 6750 wants the
    reason in a `WWW-Authenticate` header, which is how a client knows to
    refresh rather than to re-authorize.
    """
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return None, JSONResponse(
            status_code=401, content={"error": "invalid_request",
                                      "error_description": "Bearer token required."},
            headers={"WWW-Authenticate": 'Bearer realm="lenny"'})
    tok = AccessToken.authenticate(header[7:].strip())
    if tok is None:
        return None, JSONResponse(
            status_code=401, content={"error": "invalid_token",
                                      "error_description": "Token is invalid, expired or revoked."},
            headers={"WWW-Authenticate": 'Bearer realm="lenny", error="invalid_token"'})
    if not tok.has_scope(scope):
        return None, JSONResponse(
            status_code=403, content={"error": "insufficient_scope",
                                      "error_description": f"This call requires the {scope!r} scope."},
            headers={"WWW-Authenticate": f'Bearer realm="lenny", error="insufficient_scope", scope="{scope}"'})
    return tok, None


@router.get("/oauth2/loans")
async def loans(request: Request) -> Response:
    """The patron's active loans. Requires `loans:read`.

    Scoped to the patron who granted the token — there is deliberately no way to
    ask about a different patron, which is what keeps a leaked token worth one
    person's loan list rather than everyone's.
    """
    tok, err = _bearer(request, "loans:read")
    if err:
        return err

    from lenny.core.db import session as db
    rows = (
        db.query(Loan, Item)
        .join(Item, Loan.item_id == Item.id)
        .filter(Loan.patron_email_hash == tok.patron_email_hash, *Loan._active_filters())
        .all()
    )
    return JSONResponse({"loans": [
        {
            "edition_id": int(item.openlibrary_edition),
            "borrowed_at": loan.created_at.isoformat() if loan.created_at else None,
            "due_at": loan.due_date.isoformat() if loan.due_date else None,
        }
        for loan, item in rows
    ]})


@router.post("/oauth2/borrow")
async def borrow(request: Request, edition_id: int = Form(...)) -> Response:
    """Borrow on the patron's behalf. Requires `borrow`.

    Delegates to `Item.borrow`, which is the only place lending policy lives:
    open-access items are not lendable, the per-patron concurrent limit and the
    per-item copy count are enforced, and all of it happens under a
    `SELECT ... FOR UPDATE` on the Item row so two simultaneous borrows cannot
    both succeed. Calling `Loan.create` directly here would be a second,
    divergent copy of that policy — and silently skip every part of it.
    """
    tok, err = _bearer(request, "borrow")
    if err:
        return err

    item = Item.exists(edition_id)
    if item is None:
        return _error("not_found", f"This library does not hold edition {edition_id}.",
                      status=404)

    try:
        loan = item.borrow(tok.patron_email_hash, hashed=True)
    except LoanNotRequiredError:
        return _error("not_lendable",
                      "This book is open access and does not need to be borrowed.")
    except PatronLoanLimitError as exc:
        return _error("loan_limit_reached", str(exc), status=429)
    except BookUnavailableError as exc:
        return _error("unavailable", str(exc), status=409)

    return JSONResponse(status_code=201, content={
        "status": "borrowed",
        "edition_id": edition_id,
        "due_at": loan.due_date.isoformat() if loan.due_date else None,
    })
