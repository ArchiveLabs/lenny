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
import secrets
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from lenny.core import auth
from lenny.core.external_auth import valid_prompt
from lenny.routes.api import _external_auth_ready
from lenny.routes.oauth import (
    _claim_send,
    _is_top_level_navigation,
    _login_hint,
)
from lenny.core.exceptions import (
    BookUnavailableError,
    LendingNotConfiguredError,
    LoanNotRequiredError,
    OTPGenerationError,
    PatronLoanLimitError,
    RateLimitError,
)
from lenny.core.models import Item, Loan
from lenny.core.oauth2 import (
    SCOPES,
    AccessToken,
    AuthorizationCode,
    GrantRevoked,
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


# How long the signed login context is good for: the patron has to get through
# the sign-in page inside this window, and a replay of the same context sends
# nothing after it.
LOGIN_CTX_TTL = 600


def login_ctx_serializer() -> URLSafeTimedSerializer:
    """Signs what the sign-in page is allowed to know and do.

    The sign-in page is reachable two ways. `/oauth/authorize` on its own is
    the public OPDS login route, which any reader may link to and which must
    never do anything but render a form. The same page reached from here is
    part of a request that has already been checked — registered client,
    registered redirect_uri, PKCE challenge present — and may therefore say who
    is asking and send the code without a second click.

    A signature is what separates them, because nothing else can be. Both
    arrive as unauthenticated GETs carrying query parameters, so a plain flag
    would be typed by anyone, and re-deriving trust from `client_id` would
    accept any id an attacker copied out of a public authorization URL. Only a
    value this process minted proves the request came through the checks above.

    Its own salt, so a consent handle cannot be presented as a login context.
    """
    from lenny import configs
    return URLSafeTimedSerializer(configs.SEED, salt="oauth2-login-context")


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


_PROBLEMS = {
    "invalid_client": (
        "This app isn't set up with this library",
        "The app that sent you here isn't registered with this library, or it has been turned off.",
    ),
    "invalid_request": (
        "This app can't sign you in yet",
        "The app's return address isn't one this library has on file for it.",
    ),
}


def _problem_for_patron(request: Request, code: str, description: str, status: int = 400) -> Response:
    """A problem on the very first hop, before there is anywhere safe to send an
    error (RFC 6749 §4.1.2.1: do not redirect on a bad client_id or redirect_uri).

    The person looking at it is a patron who followed a link, not a program. A
    browser gets a page that says what happened and what to do; an API client
    keeps the JSON. Same status and same message text either way, so nothing is
    revealed about whether a client id exists or is disabled.
    """
    if "text/html" not in (request.headers.get("accept") or "").lower():
        return _error(code, description, status)
    heading, explanation = _PROBLEMS.get(code, ("Sign-in problem", description))
    page = request.app.templates.TemplateResponse("oauth2_error.html", {
        "request": request, "code": code, "description": description,
        "heading": heading, "explanation": explanation,
        "client_id": (request.query_params.get("client_id") or "")[:64],
    }, status_code=status)
    page.headers["X-Frame-Options"] = "DENY"
    page.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return page


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
    prompt: Optional[str] = None,
    login_hint: Optional[str] = None,
) -> Response:
    """Begin an authorization request.

    Sends the patron to log in if they have no Lenny session, then asks them to
    approve the client's requested scopes. `prompt=login` or `select_account`
    (OIDC Core §3.1.2.1) ignores an existing session and has the patron sign in
    again, so a different account can be chosen.
    """
    client = OAuthClient.get(client_id or "")
    if client is None:
        return _problem_for_patron(request, "invalid_client", "Unknown client_id.")
    if not client.allows_redirect(redirect_uri or ""):
        # Not redirected back — see the note above.
        return _problem_for_patron(request, "invalid_request",
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

    fresh = valid_prompt(prompt)
    hint = None if fresh else _login_hint(login_hint)
    email = None if fresh else _authenticated_patron(request)

    # A session for a different address than the consumer named cannot be used
    # to answer this request. Loans key on `patron_email_hash`, a SHA-256 of
    # the lowercased address (`lenny/core/utils.hash_email`), so a grant
    # recorded against the session would hand the consumer a working token for
    # a loan it can never read — nothing errors, the book simply never appears.
    # The consumer's address wins and the patron proves it with a code.
    if hint and email and hash_email(email) != hash_email(hint):
        logger.info("Ignoring a Lenny session that does not match the "
                    "consumer's login_hint; the hinted address is binding.")
        email = None

    # When the address is already known, the code goes out now and the consent
    # screen carries the field that redeems it: one screen that states the
    # request and takes the answer, instead of address -> code -> consent.
    #
    # Not reachable in external-IdP mode, which has no OTP at all, and gated on
    # the same navigation check as every other send (see `_is_top_level_
    # navigation`) so an embedded URL cannot spend one.
    pending_email = None
    if not email and hint and not _external_auth_ready() \
            and _is_top_level_navigation(request):
        client_ip = request.client.host if request.client else "unknown"
        # Keyed on the authorization request itself, so a reload re-renders the
        # screen without mailing a second code.
        if not _claim_send(f"{client.client_id}:{code_challenge}"):
            pending_email = hint
        else:
            try:
                auth.OTP.issue(hint, client_ip)
            except Exception as exc:
                # Fall through to the ordinary login hop, which renders the
                # address form and reports the reason properly.
                logger.warning("Could not send a code on arrival for %s: %s",
                               client.client_id, exc)
            else:
                pending_email = hint

    if not email and not pending_email:
        # No Lenny session yet, or the client asked for a fresh sign-in. Send
        # them through login and come back here afterwards with the request
        # intact. `prompt` is deliberately NOT part of the return trip, or the
        # patron would be asked to sign in again forever.
        this_request = f"/v1/api/oauth2/authorize?{urlencode(_echo(request))}"
        # What the sign-in page may say and do, signed so it cannot be edited
        # or invented. `login_ctx_serializer` explains why a signature is the
        # only thing that can carry this.
        #
        # The client's name and the resolved scopes travel inside it so the
        # page can lead with who is asking and what for, rather than demanding
        # a credential before saying why. Both are taken from the registration
        # and from `resolve_scope` above — never echoed from the query — so the
        # sentence a patron reads is the operator's, not the caller's.
        ctx = {
            "j": secrets.token_urlsafe(12),
            "c": client.client_id,
            "n": client.name,
            "s": granted_scope,
        }
        # RFC 6749 §3.1.2.1 `login_hint`: the consumer has already proved this
        # address, so the patron should not have to produce it again.
        #
        # Dropped entirely on a fresh sign-in. `prompt=select_account` is what
        # "Not you? Use a different account" sends, and the whole point of that
        # link is to get away from this address — pre-filling it would undo the
        # click and mailing it would be worse.
        if not fresh and (hint := _login_hint(login_hint)):
            ctx["h"] = hint
        login = {
            "redirect_uri": this_request,
            "ctx": login_ctx_serializer().dumps(ctx),
        }
        if fresh:
            login["prompt"] = fresh
        redirect = RedirectResponse(
            url=f"/v1/api/oauth/authorize?{urlencode(login)}", status_code=303)
        if fresh:
            # Drop the old login so it cannot be picked up again on the way back.
            redirect.delete_cookie(key="session", path="/", secure=True, samesite="Lax")
        return redirect

    # The form carries one opaque, signed handle instead of the request's
    # parameters. Two things follow: the POST cannot be fed a different
    # client_id or scope than the patron was shown, and it needs no second copy
    # of the validation above.
    # A random id makes the handle single-use: it is recorded when redeemed, so
    # a replay finds it spent. Without it one consent click authorised an
    # unbounded number of grants for the handle's whole lifetime, and clicking
    # "Not now" invalidated nothing.
    # `p` is the address the grant will be recorded against, and in the
    # send-on-arrival case it is the consumer's hint rather than any session.
    # `e` is present only while that address is still unproven: it is what
    # tells the POST to require a code, and it is inside the signature so the
    # address cannot be swapped between showing the screen and answering it.
    subject = email or pending_email
    handle = _consent_serializer().dumps({
        "j": secrets.token_urlsafe(16),
        "c": client.client_id,
        "r": redirect_uri,
        "s": granted_scope,
        "st": state or "",
        "cc": code_challenge,
        "m": code_challenge_method,
        "p": hash_email(subject),
        **({"e": pending_email} if pending_email else {}),
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
        # This node's own hostname. Deliberately not a new "library name"
        # setting: the consent sentence needs to name which library is being
        # borrowed from, and the node already knows its public address. An
        # operator-set display name would be nicer and is a config decision
        # nobody has made.
        "node_host": urlparse(issuer_url(request)).hostname or "this library",
        "request_handle": handle,
        "email": subject,
        # Present only while the address is unproven: the screen then carries
        # the code field instead of a "signed in as" line.
        "needs_code": bool(pending_email),
        # "Not you?": the same request, asking for a fresh sign-in.
        #
        # Offered only when the consumer named nobody. With a `login_hint`,
        # signing in as somebody else does not change which address the loan is
        # written under — it just produces one the consumer cannot read — so
        # the way out of a wrong address is to decline, not to switch.
        "switch_url": None if hint else "/v1/api/oauth2/authorize?" + urlencode(
            {**_echo(request), "prompt": "select_account"}),
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


def _render_consent(request: Request, payload: dict, handle: str,
                    error: Optional[str] = None) -> Response:
    """Re-draw the consent screen for a handle that is still live.

    Only reached when a patron mistyped the code. It rebuilds the page from the
    signed handle rather than from the form, so a retry cannot quietly change
    the client, the scopes or the address — and it does not mint a new handle,
    because the one they hold has not been spent.
    """
    client = OAuthClient.get(payload["c"])
    scopes = [(s, SCOPES[s]) for s in payload["s"].split() if s in SCOPES]
    page = request.app.templates.TemplateResponse("oauth2_consent.html", {
        "request": request,
        "client_name": client.name if client else "the application",
        "scopes": scopes,
        "redirect_host": urlparse(payload["r"]).netloc,
        "node_host": urlparse(issuer_url(request)).hostname or "this library",
        "request_handle": handle,
        "email": payload.get("e"),
        "needs_code": bool(payload.get("e")),
        "switch_url": None,
        "error": error,
    }, status_code=200)
    page.headers["X-Frame-Options"] = "DENY"
    page.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return page


def _echo(request: Request) -> dict:
    """The authorization request's own parameters, for round-tripping through
    login. Rebuilt from the parsed query so nothing extra is carried along."""
    keep = ("client_id", "redirect_uri", "response_type", "scope", "state",
            "code_challenge", "code_challenge_method", "login_hint")
    return {k: v for k, v in request.query_params.items() if k in keep}


@router.post("/oauth2/authorize")
async def authorize_decision(
    request: Request,
    request_handle: str = Form(..., alias="request"),
    decision: str = Form("deny"),
    otp: Optional[str] = Form(None),
) -> Response:
    """Record the patron's decision and redirect back to the client.

    Everything about the request comes from the signed handle minted at GET
    time, so this cannot be fed different parameters than the patron approved.

    When the handle carries `e`, the patron had no session when the screen was
    drawn and this one POST does both halves of RFC 6749 §4.1.1 — authenticate
    the resource owner, then record the authorization — in that order. The code
    is what proves the address; the address itself was fixed at GET time and is
    inside the signature, so it cannot be swapped here.
    """
    try:
        payload = _consent_serializer().loads(request_handle, max_age=_CONSENT_TTL)
    except BadSignature:
        return _error("invalid_request",
                      "This approval is no longer valid. Start the sign-in again.")

    redirect_uri = payload["r"]
    state = payload.get("st") or None
    pending_email = payload.get("e")
    session_cookie = None

    # Declining is never conditional on proving who you are. It also has to
    # spend the handle, or replaying it with decision=allow would quietly
    # overturn the refusal.
    if decision != "allow":
        if not _spend_consent(payload.get("j")):
            return _error("invalid_request",
                          "This approval has already been used. Start the sign-in again.")
        return _redirect_error(redirect_uri, "access_denied",
                               "The patron declined this request.", state)

    if pending_email:
        # Prove the address before recording anything against it. A wrong code
        # must not spend the handle — a typo would otherwise cost the patron
        # the whole authorization request and send them back to the consumer to
        # start again. Guessing is bounded by `OTP.verify`'s own attempt limit,
        # not by burning the handle.
        client_ip = request.client.host if request.client else "unknown"
        error = None
        if not (otp or "").strip():
            error = "Enter the code we emailed you."
        else:
            try:
                session_cookie = auth.OTP.authenticate(
                    pending_email, otp.strip(), client_ip)
            except LendingNotConfiguredError as e:
                # This one names an environment variable and the admin panel.
                # It is addressed to an operator and the person reading it is a
                # patron, so log the detail and say something they can act on.
                logger.error("Consent screen could not verify a code: %s", e)
                error = ("This library is not set up to lend right now. "
                         "Please tell your librarian.")
            except (RateLimitError, OTPGenerationError) as e:
                error = str(e)
            if not session_cookie and not error:
                error = "That code is not valid. Check the email and try again."
        if error:
            return _render_consent(request, payload, request_handle, error=error)
        email = pending_email
    else:
        email = _authenticated_patron(request)
        if not email:
            return _error("access_denied", "Your session expired. Start again.", status=401)
        # The handle is bound to the patron it was shown to. An attacker can
        # mint a valid handle by starting their own authorization; without this
        # check they could get a victim's browser to submit it and silently
        # obtain a code against the victim's account.
        if payload.get("p") != hash_email(email):
            return _error("access_denied",
                          "This approval was issued for a different account.", status=403)

    if not _spend_consent(payload.get("j")):
        return _error("invalid_request",
                      "This approval has already been used. Start the sign-in again.")

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
        done = request.app.templates.TemplateResponse("oauth2_handoff.html", {
            "request": request, "target": target,
            "client_name": client.name if client else "the application",
        })
    else:
        done = RedirectResponse(url=target, status_code=303)

    # The patron proved their address on this request, so leave them signed in
    # — otherwise borrowing through Open Library would silently cost them a
    # second code the moment they open the book here.
    if session_cookie:
        done.set_cookie(key="session", value=session_cookie,
                        max_age=auth.COOKIE_TTL, httponly=True, secure=True,
                        samesite="Lax", path="/")
    return done


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
        try:
            access, refresh, tok = AccessToken.issue(
                client_id=client.client_id,
                patron_email_hash=row.patron_email_hash,
                scope=row.scope,
                authorization_code_id=row.id,
            )
        except GrantRevoked as exc:
            # A concurrent caller replayed this code and the family was killed
            # between our claim and our issue. Report it rather than handing
            # back a 200 with a token that is already dead.
            logger.warning("Grant revoked mid-issue for client %r", client.client_id)
            return _error("invalid_grant", str(exc))

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
    # A patron's reading history is exactly what should not sit in a shared
    # cache or a proxy log, and the consumer is a backend that may well have one
    # in front of it.
    return JSONResponse({"loans": [
        {
            "edition_id": int(item.openlibrary_edition),
            "borrowed_at": loan.created_at.isoformat() if loan.created_at else None,
            "due_at": loan.due_date.isoformat() if loan.due_date else None,
        }
        for loan, item in rows
    ]}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.post("/oauth2/borrow")
async def borrow(request: Request, edition_id: int = Form(...)) -> Response:
    """Borrow on the patron's behalf. Requires `borrow`.

    Delegates to `Item.borrow`, which is the only place lending policy lives:
    open-access items are not lendable, and the per-patron concurrent limit and
    the per-item copy count are both enforced. Calling `Loan.create` directly
    here would be a second, divergent copy of that policy — and silently skip
    every part of it.

    Note that this endpoint is the reason `Item.borrow` locks the patron as well
    as the item. A browser gets one click at a time; a backend consumer holding
    a token issues borrows in parallel, which is what turned a theoretical race
    on the per-patron limit into an observed bypass.
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
