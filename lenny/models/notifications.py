from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
from sqlalchemy.sql import func
from . import Base

class Notification(Base):
    __tablename__ = 'notifications'

    notification_id = Column(Integer, primary_key=True, autoincrement=True)
    patron_id = Column(String(50), ForeignKey('patrons.patron_id', ondelete='CASCADE'), nullable=False)
    type = Column(String(50), nullable=False)
    message = Column(String, nullable=False)
    date = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    is_read = Column(Boolean, default=False, nullable=False)