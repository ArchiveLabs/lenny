from pydantic import BaseModel
from typing import Optional

class Item(BaseModel):
    identifier: str
    title: str
    item_status: str
    language: str
    is_readable: Optional[bool] = False
    is_lendable: Optional[bool] = True
    is_waitlistable: Optional[bool] = True
    is_printdisabled: Optional[bool] = False
    is_login_required: Optional[bool] = False
    num_lendable_total: Optional[int] = 0
    current_num_lendable: Optional[int] = 0
    current_waitlist_size: Optional[int] = 0

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "identifier": "book1",
                "title": "Test Book",
                "item_status": "available",
                "language": "en",
                "is_readable": False,
                "is_lendable": True,
                "is_waitlistable": False,
                "is_printdisabled": False,
                "is_login_required": False,
                "num_lendable_total": 5,
                "current_num_lendable": 5,
                "current_waitlist_size": 0,
            }
        }