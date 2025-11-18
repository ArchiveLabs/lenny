#!/usr/bin/env python3

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from lenny.routes import api
from lenny.configs import OPTIONS
from lenny import __version__ as VERSION

# -------------------------------
# ⭐ RATE LIMITER CONFIG
# -------------------------------
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

# Create limiter instance
limiter = Limiter(key_func=get_remote_address)


# -------------------------------
# ⭐ CREATE FASTAPI APP
# -------------------------------
app = FastAPI(
    title="Lenny API",
    description="Lenny: A Free, Open Source Lending System for Libraries",
    version=VERSION,
)

# Attach limiter to app state
app.state.limiter = limiter

# Add middleware
app.add_middleware(SlowAPIMiddleware)

# Handle rate limit exceptions
@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={"error": "Too many requests", "detail": str(exc)}
    )


# ----------------------------------------------------
# EXISTING CODE BELOW (UNCHANGED)
# ----------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.templates = Jinja2Templates(directory="lenny/templates")

app.include_router(api.router, prefix="/v1/api")

app.mount("/static", StaticFiles(directory="lenny/static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)
