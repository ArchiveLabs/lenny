from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class License(BaseModel):
    license_id: str
    loan_id: str
    status: str
    end: datetime
    content_id: Optional[str] = None

    class Config:
        from_attributes = True