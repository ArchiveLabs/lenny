#!/usr/bin/env python

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from lenny.configs import DB_URI, TESTING
import os

# Base class for models
Base = declarative_base()

# Dependency to provide DB session
def get_db():
    if TESTING:
        engine = create_engine("sqlite:///:memory:", connect_args={'check_same_thread': False})
        Base.metadata.create_all(bind=engine)  # Create tables for testing
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()
            engine.dispose()
    else:
        engine = create_engine(DB_URI)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

__all__ = ['Base', 'get_db']