#!/usr/bin/env python

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from lenny.configs import DB_URI, TESTING
import os
from contextlib import contextmanager

# Base class for models
Base = declarative_base()

# Define engine at module level
if TESTING:
    engine = create_engine("sqlite:///:memory:", connect_args={'check_same_thread': False})
    Base.metadata.create_all(bind=engine) # Create tables for testing
else:
    engine = create_engine(DB_URI)

# SessionLocal factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Dependency to provide DB session
def get_db():
    """Dependency function that yields a SQLAlchemy session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        if TESTING:
            # Dispose engine only in testing to ensure clean state between tests if needed
            # In production, the engine should persist
            engine.dispose()

# --- Session Factory for Batch Processing ---
@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations for batch processing."""
    session = SessionLocal() # Use the factory directly
    try:
        yield session
        # Commits are handled within process_book_file -> upload_item or update_item_access
        # No top-level commit needed here for batch processing as each item is handled individually.
    except Exception:
        session.rollback() # Rollback on error within the scope
        raise
    finally:
        session.close() # Ensure session is closed

__all__ = ['Base', 'get_db', 'session_scope', 'engine', 'SessionLocal'] # Added engine and SessionLocal