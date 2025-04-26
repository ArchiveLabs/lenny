from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class Notification(BaseModel):
    notification_id: Optional[int] = None
    patron_id: str
    type: str
    message: str
    date: Optional[datetime] = None
    is_read: Optional[bool] = False

    class Config:
        from_attributes = True