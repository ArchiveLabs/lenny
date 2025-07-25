from pydantic import BaseModel
from typing import List

class EmailRequest(BaseModel):
    email: str

class CheckoutRequest(BaseModel):
    openlibrary_editions: List[int]
    email: str

class OpenAccessRequest(BaseModel):
    item_id: int
    email: str
