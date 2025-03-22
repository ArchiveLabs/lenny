#!/usr/bin/env python3

import uvicorn
from fastapi import FastAPI
from lenny.routes import api
from lenny.configs import OPTIONS

app = FastAPI()

app.include_router(api.router, prefix="/v1/api")

if __name__ == "__main__":
    uvicorn.run("lenny.app:app", **OPTIONS)
