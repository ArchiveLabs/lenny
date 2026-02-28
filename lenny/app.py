#!/usr/bin/env python3

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from lenny.routes import api, oauth
from lenny.configs import OPTIONS
from lenny import __version__ as VERSION
from lenny.core.limiter import limiter

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_SECONDS = 3600  # 1 hour

async def _periodic_token_cleanup():
    """Background task to purge expired/used auth codes and revoked/expired refresh tokens."""
    from lenny.core.models import AuthCode, RefreshToken
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            codes = AuthCode.cleanup_expired()
            tokens = RefreshToken.cleanup_expired()
            if codes or tokens:
                logger.info(f"OAuth cleanup: removed {codes} auth codes, {tokens} refresh tokens")
        except Exception:
            logger.exception("OAuth cleanup failed")

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_periodic_token_cleanup())
    yield
    task.cancel()

app = FastAPI(
    title="Lenny API",
    description="Lenny: A Free, Open Source Lending System for Libraries",
    version=VERSION,
    lifespan=lifespan,
)

# Rate Limiter
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# App-level CORS should be a setting, as to not
# conflict with CORS handled by nginx
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.templates = Jinja2Templates(directory="lenny/templates")

app.include_router(api.router, prefix="/v1/api")
app.include_router(oauth.router, prefix="/v1/oauth")

app.mount("/static", StaticFiles(directory="lenny/static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)
