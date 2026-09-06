#!/usr/bin/env python3

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from lenny.routes import api
from lenny.routes import oauth as oauth_routes
from lenny.configs import OPTIONS
from lenny.core.db import session as db_session
from lenny import __version__ as VERSION

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

app.templates = Jinja2Templates(directory="lenny/templates")

app.include_router(api.router, prefix="/v1/api")
app.include_router(oauth_routes.router, prefix="/v1/api")

app.mount("/static", StaticFiles(directory="lenny/static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)
