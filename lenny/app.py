#!/usr/bin/env python3

from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from lenny.routes import api
from lenny.models import get_db, Base
from lenny.configs import OPTIONS
from sqlalchemy.orm import Session
from lenny.core.items import initialize_minio_buckets

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown"""
    # Startup logic
    initialize_minio_buckets()
    yield
    # Shutdown logic (if needed)

app = FastAPI(title="Lenny", lifespan=lifespan)
app.include_router(api.router, prefix="/v1/api")
app.mount('/static', StaticFiles(directory='lenny/static'), name='static')

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)