import pytest
from unittest.mock import Mock, patch
from lenny.core.items import upload_item, delete_item, update_item_access
from lenny.models.items import Item
from lenny.models import get_db, Base
from sqlalchemy.orm import Session
import os

@pytest.fixture
def db_session():
    with next(get_db()) as session:
        Base.metadata.create_all(bind=session.bind)  # Ensuring tables are created
        yield session

@pytest.fixture
def mock_minio():
    mock = Mock()
    mock.put_object = Mock(return_value=None)
    mock.remove_object = Mock(return_value=None)
    mock.get_object = Mock(return_value=Mock(read=lambda: b"Test content"))
    with patch("lenny.core.items.Minio", new=lambda *args, **kwargs: mock):
        yield mock

def test_upload_item_success(db_session, mock_minio):
    mock_minio.put_object.return_value = None
    temp_path = "test_file.txt"
    with open(temp_path, "w") as f:
        f.write("Test content")
    item = upload_item(
        db_session,
        "book1",
        "Test Book",
        "available",
        "en",
        temp_path,
    )
    os.remove(temp_path)
    assert item.identifier == "book1"
    assert item.s3_public_path == "s3://lenny-public/book1.txt"
    assert item.s3_protected_path is None
    assert mock_minio.put_object.call_count == 1

def test_upload_item_borrowable(db_session, mock_minio):
    mock_minio.put_object.return_value = None
    mock_minio.get_object.return_value = Mock(read=lambda: b"Test content")
    temp_path = "test_file.txt"
    with open(temp_path, "w") as f:
        f.write("Test content")
    item = upload_item(
        db_session,
        "book2",
        "Borrowable Book",
        "borrowable",
        "en",
        temp_path,
    )
    os.remove(temp_path)
    assert item.identifier == "book2"
    assert item.s3_public_path == "s3://lenny-public/book2.txt"
    assert item.s3_protected_path == "s3://lenny-protected/book2.txt"
    assert mock_minio.put_object.call_count == 2

def test_delete_item_success(db_session, mock_minio):
    item = Item(
        identifier="book1", 
        title="Test Book", 
        item_status="available", 
        language="en", 
        num_lendable_total=1, 
        current_num_lendable=1,
        s3_public_path="s3://lenny-public/book1.pdf"
    )
    db_session.add(item)
    db_session.commit()
    mock_minio.remove_object.return_value = None
    result = delete_item(db_session, "book1")
    assert result is True
    assert db_session.query(Item).filter(Item.identifier == "book1").first() is None
    assert mock_minio.remove_object.call_count == 1

def test_delete_item_not_found(db_session, mock_minio):
    mock_minio.remove_object.return_value = None
    result = delete_item(db_session, "nonexistent")
    assert result is False

def test_update_item_access_open(db_session, mock_minio):
    item = Item(
        identifier="book1", 
        title="Test Book", 
        item_status="borrowable", 
        language="en", 
        num_lendable_total=1, 
        current_num_lendable=1, 
        s3_public_path="s3://lenny-public/book1.pdf",
        s3_protected_path="s3://lenny-protected/book1.pdf"
    )
    db_session.add(item)
    db_session.commit()
    mock_minio.remove_object.return_value = None
    result = update_item_access(db_session, "book1", True)
    assert result is True
    updated_item = db_session.query(Item).filter(Item.identifier == "book1").first()
    assert updated_item.s3_protected_path is None
    assert mock_minio.remove_object.call_count == 1

def test_update_item_access_protected(db_session, mock_minio):
    item = Item(
        identifier="book1", 
        title="Test Book", 
        item_status="available", 
        language="en", 
        num_lendable_total=1, 
        current_num_lendable=1, 
        s3_public_path="s3://lenny-public/book1.pdf"
    )
    db_session.add(item)
    db_session.commit()
    
    # Setup mocks
    mock_minio.put_object.return_value = None
    mock_minio.get_object.return_value = Mock(read=lambda: b"Test content", close=Mock(), release_conn=Mock())
    
    # Mock stat_object to return file size info
    stat_mock = Mock()
    stat_mock.size = 1024
    mock_minio.stat_object = Mock(return_value=stat_mock)
    
    result = update_item_access(db_session, "book1", False)

    assert result is True
    updated_item = db_session.query(Item).filter(Item.identifier == "book1").first()
    assert updated_item.s3_protected_path == "s3://lenny-protected/book1.pdf"
    assert mock_minio.put_object.call_count == 1
    assert mock_minio.stat_object.call_count == 1