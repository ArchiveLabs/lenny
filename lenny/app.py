#!/usr/bin/env python3

from fastapi import FastAPI, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from lenny.routes import api
from lenny.models import get_db, Base, engine, session_scope, SessionLocal
from lenny.configs import OPTIONS
from sqlalchemy.orm import sessionmaker, Session
from lenny.core.items import initialize_minio_buckets, preload_books
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown"""
    # Startup logic
    # print("Running lifespan startup tasks...")
    initialize_minio_buckets()
    # print("MinIO buckets initialized.")
    # Preload books using the session_scope context manager
    # print("Starting book preloading...")
    try:
        # Note: preload_books itself uses session_scope, so we pass the factory
        preload_books(session_scope)
        # print("Book preloading finished.")
    except Exception as e:
        # print(f"Error during book preloading: {e}")
        pass  # Consider logging this error
    yield
    # Shutdown logic (if needed)
    # print("Running lifespan shutdown tasks...")

app = FastAPI(
    title="Lenny API",
    description="Lenny: A Free, Open Source Lending System for Libraries",
    version="0.1.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory="lenny/static"), name="static")

app.include_router(api.router, prefix="/v1/api")

# Root endpoint (optional, can be removed if api router handles root)
@app.get("/", include_in_schema=False)
async def root(request: Request):
    return HTMLResponse("<html><body><h1>Lenny API</h1></body></html>")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)