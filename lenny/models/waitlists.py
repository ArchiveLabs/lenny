from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func
from . import Base
from sqlalchemy import UniqueConstraint

class Waitlist(Base):
    __tablename__ = 'waitlists'

    waitlist_id = Column(Integer, primary_key=True, autoincrement=True)
    item_identifier = Column(String(50), ForeignKey('items.identifier', ondelete='CASCADE'), nullable=False)
    patron_id = Column(String(50), ForeignKey('patrons.patron_id', ondelete='CASCADE'), nullable=False)
    position = Column(Integer, nullable=False)
    joined_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    status = Column(String(20), nullable=False)
    __table_args__ = (UniqueConstraint('item_identifier', 'patron_id', name='unique_waitlist'),)