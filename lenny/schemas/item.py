#!/usr/bin/env python
"""
    Item Schema for Lenny,
    including the definition of the Item model and its attributes.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from pydantic import BaseModel
from typing import Optional

class Item(BaseModel):
    
    openlibrary_edition: int
    encrypted: Optional[bool] = False
    
    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "openlibrary_edition": 1234567890,
                "encrypted": False
            }
        }
        
