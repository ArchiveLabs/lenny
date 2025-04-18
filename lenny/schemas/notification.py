from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class Notification(BaseModel):
    notification_id: Optional[int] = None
    patron_id: str = Field(..., min_length=1, max_length=50)
    type: str = Field(..., min_length=1, max_length=50)
    message: str = Field(..., min_length=1)
    date: Optional[datetime] = None
    is_read: bool = Field(default=False)

    class Config:
        from_attributes = True