#!/usr/bin/env python
"""
    Pydantic schemas for the /admin/ol/* endpoints.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional


class OLLoginRequest(BaseModel):
    """Payload for `POST /admin/ol/login`.

    `email` is an IA / OL account login. `password` is bounded to reject
    oversized payloads (IA passwords are much shorter in practice).
    `replace=True` confirms the operator wants to overwrite existing credentials.
    """
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)
    replace: Optional[bool] = False

    @field_validator("email")
    @classmethod
    def _email_shape(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or "." not in v.split("@", 1)[-1]:
            raise ValueError("Email must be a valid address.")
        return v

    class Config:
        json_schema_extra = {
            "example": {
                "email": "librarian@example.org",
                "password": "…",
                "replace": False,
            }
        }
