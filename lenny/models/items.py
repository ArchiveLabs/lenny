from sqlalchemy import Column, String, Boolean, Integer, DateTime
from sqlalchemy.sql import func
from . import Base

class Item(Base):
    __tablename__ = 'items'

    identifier = Column(String(100), primary_key=True)
    title = Column(String(255), nullable=False)
    item_status = Column(String(20), nullable=False)
    language = Column(String(10), nullable=False)
    is_readable = Column(Boolean, default=False, nullable=False)
    is_lendable = Column(Boolean, default=True, nullable=False)
    is_waitlistable = Column(Boolean, default=True, nullable=False)
    is_printdisabled = Column(Boolean, default=False, nullable=False)
    is_login_required = Column(Boolean, default=False, nullable=False)
    num_lendable_total = Column(Integer, nullable=False)
    current_num_lendable = Column(Integer, nullable=False)
    current_waitlist_size = Column(Integer, default=0, nullable=False)
    s3_public_path = Column(String)
    s3_protected_path = Column(String)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())