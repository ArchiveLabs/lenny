#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
    tests.test_packaging
    ~~~~~~~~~~~~~~~~~~~~

    This module tests the core items of the Lenny package.

    :copyright: (c) 2015 by Authors.
    :license: see LICENSE for more details.
"""

import pytest 
from lenny.models import Base

@pytest.fixture
def db_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)  # Corrected: use engine instead of session.bind
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine) # Clean up: drop tables after test

# Add necessary imports for testing itemsUpload
import io
from unittest.mock import MagicMock, patch
from fastapi import UploadFile, HTTPException, status
from pathlib import Path
from lenny.core.itemsUpload import upload_items
from lenny.models.items import Item
from botocore.exceptions import ClientError

# Mock S3 client for tests
@pytest.fixture
def mock_s3_client():
    with patch('lenny.core.itemsUpload.s3') as mock_s3:
        yield mock_s3

# Mock db for tests to avoid using global db session from lenny.models.db
@pytest.fixture
def mock_db_session_in_upload(db_session): # Renamed to avoid conflict with the main db_session fixture
    with patch('lenny.core.itemsUpload.db', db_session):
        yield db_session

def create_mock_upload_file(filename: str, content: bytes = b"test content", content_type: str = "text/plain") -> UploadFile:
    """Helper function to create a mock UploadFile."""
    file_like_object = io.BytesIO(content)
    # Pass content_type via headers argument during UploadFile instantiation
    headers = {"content-type": content_type}
    upload_file = UploadFile(filename=filename, file=file_like_object, headers=headers)
    return upload_file

def test_upload_single_item_success(db_session, mock_s3_client):
    """Test successful upload of a single item."""
    openlibrary_edition = 12345
    encrypted = False
    mock_file = create_mock_upload_file("test.txt")
    files = [mock_file]

    upload_items(openlibrary_edition, encrypted, files, db_session=db_session)

    mock_s3_client.upload_fileobj.assert_called_once_with(
        mock_file.file,
        "bookshelf-public",
        f"{openlibrary_edition}.txt",
        ExtraArgs={'ContentType': mock_file.content_type}
    )
    
    item_in_db = db_session.query(Item).filter_by(openlibrary_edition=openlibrary_edition).first()
    assert item_in_db is not None
    assert item_in_db.s3_filepath == f"bookshelf-public/{openlibrary_edition}.txt"
    assert item_in_db.encrypted == encrypted

def test_upload_multiple_items_success(db_session, mock_s3_client):
    """Test successful upload of multiple items."""
    openlibrary_edition = 67890
    encrypted = True
    mock_file1 = create_mock_upload_file("test1.pdf", content_type="application/pdf")
    mock_file2 = create_mock_upload_file("test2.epub", content_type="application/epub+zip")
    files = [mock_file1, mock_file2]

    upload_items(openlibrary_edition, encrypted, files, db_session=db_session)

    assert mock_s3_client.upload_fileobj.call_count == 2
    
    # Check first file upload
    mock_s3_client.upload_fileobj.assert_any_call(
        mock_file1.file,
        "bookshelf-encrypted",
        f"{openlibrary_edition}.pdf",
        ExtraArgs={'ContentType': mock_file1.content_type}
    )
    # Check second file upload
    mock_s3_client.upload_fileobj.assert_any_call(
        mock_file2.file,
        "bookshelf-encrypted",
        f"{openlibrary_edition}.epub",
        ExtraArgs={'ContentType': mock_file2.content_type}
    )

    items_in_db = db_session.query(Item).filter_by(openlibrary_edition=openlibrary_edition).all()
    assert len(items_in_db) == 2
    s3_paths_in_db = {item.s3_filepath for item in items_in_db}
    expected_s3_paths = {
        f"bookshelf-encrypted/{openlibrary_edition}.pdf",
        f"bookshelf-encrypted/{openlibrary_edition}.epub"
    }
    assert s3_paths_in_db == expected_s3_paths
    for item in items_in_db:
        assert item.encrypted == encrypted

def test_upload_item_no_filename(db_session, mock_s3_client):
    """Test upload attempt with a file that has no filename."""
    openlibrary_edition = 11122
    encrypted = False
    # Create a mock UploadFile with filename as None or empty string
    mock_file_no_name = UploadFile(filename="", file=io.BytesIO(b"test")) 
    files = [mock_file_no_name]

    upload_items(openlibrary_edition, encrypted, files, db_session=db_session)

    mock_s3_client.upload_fileobj.assert_not_called()
    item_in_db = db_session.query(Item).filter_by(openlibrary_edition=openlibrary_edition).first()
    assert item_in_db is None

def test_upload_item_s3_client_error(db_session, mock_s3_client):
    """Test S3 upload failure (ClientError)."""
    openlibrary_edition = 33445
    encrypted = False
    mock_file = create_mock_upload_file("failure.txt")
    files = [mock_file]

    # Simulate S3 ClientError
    mock_s3_client.upload_fileobj.side_effect = ClientError({"Error": {"Message": "S3 Upload Failed"}}, "upload_fileobj")

    with pytest.raises(HTTPException) as excinfo:
        upload_items(openlibrary_edition, encrypted, files, db_session=db_session)
    
    assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Failed to upload 'failure.txt' to S3" in excinfo.value.detail
    
    item_in_db = db_session.query(Item).filter_by(openlibrary_edition=openlibrary_edition).first()
    assert item_in_db is None # Ensure rollback happened

def test_upload_item_general_exception_during_upload(db_session, mock_s3_client):
    """Test a general exception during the file processing loop."""
    openlibrary_edition = 55667
    encrypted = True
    mock_file = create_mock_upload_file("general_error.doc")
    files = [mock_file]

    # Simulate a general error
    mock_s3_client.upload_fileobj.side_effect = Exception("Something went wrong")

    with pytest.raises(HTTPException) as excinfo:
        upload_items(openlibrary_edition, encrypted, files, db_session=db_session)

    assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "An error occurred with file 'general_error.doc'" in excinfo.value.detail

    item_in_db = db_session.query(Item).filter_by(openlibrary_edition=openlibrary_edition).first()
    assert item_in_db is None # Ensure rollback happened

def test_upload_item_db_commit_failure(db_session, mock_s3_client):
    """Test database commit failure after successful S3 uploads."""
    openlibrary_edition = 77889
    encrypted = False
    mock_file = create_mock_upload_file("commit_fail.zip")
    files = [mock_file]

    # Mock the commit method of the db_session to raise an exception
    original_commit = db_session.commit
    db_session.commit = MagicMock(side_effect=Exception("DB Commit Failed"))

    with pytest.raises(HTTPException) as excinfo:
        upload_items(openlibrary_edition, encrypted, files, db_session=db_session)
    
    assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "Database commit failed: DB Commit Failed" in excinfo.value.detail
    
    # Restore original commit method to not affect other tests
    db_session.commit = original_commit
    
    # We can't easily check if the item was added and then rolled back without more intricate db mocking
    # or checking logs, but the key is that the HTTP exception for commit failure is raised.
    # And s3 upload should have been called
    mock_s3_client.upload_fileobj.assert_called_once()