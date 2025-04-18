from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class Waitlist(BaseModel):
    waitlist_id: Optional[int] = None
    item_identifier: str
    patron_id: str
    position: int
    joined_at: datetime
    status: str

    class Config:
        from_attributes = True