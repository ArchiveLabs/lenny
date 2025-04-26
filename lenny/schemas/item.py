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


class ItemUpdate(BaseModel):
    title: Optional[str] = None
    item_status: Optional[str] = None
    language: Optional[str] = None
    is_readable: Optional[bool] = None
    is_lendable: Optional[bool] = None
    is_waitlistable: Optional[bool] = None
    is_printdisabled: Optional[bool] = None
    is_login_required: Optional[bool] = None
    num_lendable_total: Optional[int] = None
    current_num_lendable: Optional[int] = None
    current_waitlist_size: Optional[int] = None

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "title": "Updated Test Book Title",
                "current_num_lendable": 4,
            }
        }

class ItemDelete(BaseModel):
    identifier: str

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "identifier": "book1"
            }
        }

class ItemDeleteResponse(BaseModel):
    identifier: str
    title: str
    status: str

    class Config:
        from_attributes = True
        json_schema_extra = {
            "example": {
                "identifier": "book1",
                "title": "Test Book",
                "status": "deleted"
            }
        }