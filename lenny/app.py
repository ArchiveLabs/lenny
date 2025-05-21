#!/usr/bin/env python3

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from lenny.routes import api
from lenny.configs import OPTIONS
from lenny import __version__ as VERSION

app = FastAPI(
    title="Lenny API",
    description="Lenny: A Free, Open Source Lending System for Libraries",
    version=VERSION,
)

app.include_router(api.router, prefix="/v1/api")

app.mount("/static", StaticFiles(directory="Lenny/static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("lenny.app:app", **OPTIONS)
