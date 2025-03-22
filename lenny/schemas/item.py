#!/usr/bin/env python
from sqlalchemy import Column, Integer, String, Boolean
from lenny.models import, db, Base

class Item(Base):
    __tablename__ = "items"
    identifier = Column(String, primary_key=True)
    is_lendable = Column(Boolean, default=True)
    num_lendable_total = Column(Integer, default=5)
