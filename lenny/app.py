#!/usr/bin/env python3

import logging

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from lenny.routes import api
from lenny.routes import oauth as oauth_routes
from lenny.routes import oauth2 as oauth2_routes
from lenny.configs import FORWARDED_ALLOW_IPS, OPTIONS
from lenny.core.db import session as db_session
from lenny import __version__ as VERSION

_log = logging.getLogger(__name__)

app = FastAPI(
    title="Lenny API",
    description="Lenny: A Free, Open Source Lending System for Libraries",
    version=VERSION,
)

# `db_session` is a scoped_session shared across requests on the same worker
# thread. A DB error leaves its transaction aborted; without a teardown,
# every later request on that thread inherits the poisoned transaction and
# fails, even for unrelated queries. Removing it after each request forces
# a fresh session next time.
@app.middleware("http")
async def cleanup_db_session(request, call_next):
    try:
        return await call_next(request)
    finally:
        db_session.remove()


# X-Forwarded-For misconfiguration is silent in both directions, which is why
# ArchiveLabs/lenny#210 survived from the day proxy headers were enabled:
#
#   trust too broad ('*')  -> uvicorn takes the LEFTMOST hop, so a caller picks
#                             its own IP and every IP check becomes advisory.
#   trust too narrow       -> uvicorn trusts no proxy, so every patron collapses
#                             onto the nginx address: one shared identity, one
#                             shared OTP rate-limit bucket, and a session binding
#                             that matches from anywhere.
#
# Neither raises, logs, or changes a status code. This warns once per worker on
# the first request that proves the chain is being resolved wrongly: with a
# correct configuration `request.client.host` is the RIGHTMOST entry of the
# header nginx built with $proxy_add_x_forwarded_for.
#
# Ordering note, because "middleware ordering" here is really two separate
# things and only one of them is about this file. uvicorn applies
# ProxyHeadersMiddleware in `Config.load` — `ProxyHeadersMiddleware(loaded_app,
# trusted_hosts=...)` — so it wraps the ENTIRE Starlette stack from outside.
# `scope["client"]` is therefore already resolved before any middleware below
# runs, and where this sits relative to the others does not affect what it sees.
# Add-order only decides nesting AMONG Starlette's own middleware (that is what
# makes a CORS override need to be added last, to wrap outermost).
#
# One warning per worker process, so the default --workers=3 can emit up to
# three. That is intended: this is a startup-class diagnostic, not per-request
# logging.
_xff_warned = False


@app.middleware("http")
async def warn_on_untrusted_proxy_chain(request, call_next):
    global _xff_warned
    if not _xff_warned:
        xff = request.headers.get("x-forwarded-for")
        client = request.client.host if request.client else None
        if xff and client:
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            if hops and client != hops[-1]:
                _xff_warned = True
                _log.warning(
                    "X-Forwarded-For is not being resolved as expected: "
                    "client.host=%r but the last proxy hop is %r (chain=%r). "
                    "LENNY_FORWARDED_ALLOW_IPS=%r. If client.host matches the "
                    "first hop, the range is too broad and callers can spoof "
                    "their IP; if it matches neither, it is too narrow and every "
                    "patron is sharing one identity. See ArchiveLabs/lenny#210.",
                    client, hops[-1], xff, FORWARDED_ALLOW_IPS,
                )
    return await call_next(request)


if FORWARDED_ALLOW_IPS.strip() == "*":
    _log.warning(
        "LENNY_FORWARDED_ALLOW_IPS='*' trusts every peer, so uvicorn takes the "
        "first X-Forwarded-For entry — a value the caller controls. Session-cookie "
        "IP binding and OTP IP binding are unenforceable in this configuration. "
        "Set it to your Docker network (see docker/configure.sh)."
    )

# CORS is permissive at the app layer because nginx enforces the real security
# boundary: `location /v1/api/admin { return 403; }` blocks all cross-origin
# admin calls before they reach this process. Patron endpoints (OPDS, borrow)
# are intentionally accessible from any origin (OPDS clients, bookreaders, etc.).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Added after CORSMiddleware, so it is the outermost layer and sees the response
# last — which is what lets it override the CORS headers that middleware sets.
@app.middleware("http")
async def no_cross_origin_reads_of_oauth2_authorize(request, call_next):
    """Keep the consent screen out of reach of other origins' JavaScript.

    The app-wide policy is `allow_origin_regex=".*"` with
    `allow_credentials=True`, which reflects whatever Origin asks. That is a
    deliberate choice for OPDS clients, but this endpoint is cookie
    authenticated and renders a consent handle, so reflecting an attacker's
    origin would let their page read the handle and submit it — an
    authorization code with no click. `SameSite=Lax` happens to prevent that
    today, but it is a cookie set in another module and nothing here owns it.

    Nothing should ever read this page with JavaScript; it is a navigation
    target. Saying so explicitly costs nothing.
    """
    response = await call_next(request)
    if request.url.path == "/v1/api/oauth2/authorize":
        response.headers["Access-Control-Allow-Origin"] = "null"
        response.headers["Access-Control-Allow-Credentials"] = "false"
    return response


app.templates = Jinja2Templates(directory="lenny/templates")

app.include_router(api.router, prefix="/v1/api")
app.include_router(oauth_routes.router, prefix="/v1/api")
app.include_router(oauth2_routes.router, prefix="/v1/api")


# RFC 8414 requires this at the origin root, not under a path prefix — a client
# given only "https://some-lenny.example.org" must be able to discover the
# endpoints. That discoverability is what lets Open Library talk to a Lenny node
# it has never seen before without anyone provisioning it by hand.
@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server_metadata(request: Request):
    from fastapi.responses import JSONResponse
    from lenny import configs
    from lenny.core.api import LennyAPI
    from lenny.core.oauth2 import SCOPES

    # Built from the node's configured public URL, never the request — see
    # `issuer_url`, which explains why the Host header must not decide a
    # security-relevant identifier. A mismatch is warned about, not patched.
    from lenny.routes.oauth2 import issuer_url
    issuer = issuer_url(request)
    base = f"{issuer}/v1/api"
    return JSONResponse({
        "issuer": issuer,
        "authorization_endpoint": f"{base}/oauth2/authorize",
        "token_endpoint": f"{base}/oauth2/token",
        "revocation_endpoint": f"{base}/oauth2/revoke",
        # No registration_endpoint: clients are registered by the operator, so
        # advertising one would send a consumer to something that does not exist.
        "scopes_supported": sorted(SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        # RFC 9207 — tells a client it can rely on `iss` to tell nodes apart.
        "authorization_response_iss_parameter_supported": True,
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic", "client_secret_post",
            # RFC 8252: a native app has no secret to keep, and PKCE is
            # mandatory here, which is what makes that safe.
            "none",
        ],
        "service_documentation": "https://github.com/ArchiveLabs/lenny",
    })


app.mount("/static", StaticFiles(directory="lenny/static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)
