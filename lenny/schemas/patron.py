from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime

class Patron(BaseModel):
    patron_id: str
    name: str
    email: EmailStr
    accessibility: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True