#!/usr/bin/env python
"""
    Models Configurations for Lenny,
    including handling database connections and ORM setup.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""
import json
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base 
from lenny.configs import DB_URI, DEBUG

Base = declarative_base()

# Import all models here to ensure they are registered with Base
from . import items

# Configure Database Connection
engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
db = scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False))


def init_db(engine_to_init=engine):
    """Initializes the database and creates tables."""
    Base.metadata.create_all(bind=engine_to_init)


__all__ = ["Base", "db", "engine", "items", "init_db"]
