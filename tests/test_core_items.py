import pytest
from unittest.mock import Mock, patch, MagicMock
from lenny.core.items import (
    upload_item, delete_item, update_item_access,
    extract_epub_metadata, extract_pdf_metadata, process_book_file
)
from lenny.models.items import Item
from lenny.models import get_db, Base
from lenny.configs import S3_CONFIG
from sqlalchemy.orm import Session
import os
from minio.error import S3Error

@pytest.fixture
def db_session():
    with next(get_db()) as session:
        Base.metadata.create_all(bind=session.bind)  
        yield session

@pytest.fixture
def mock_minio():
    mock = MagicMock()
    mock.put_object = MagicMock(return_value=None)
    mock.remove_object = MagicMock(return_value=None)
    mock.get_object = MagicMock(return_value=Mock(read=lambda: b"Test content", close=Mock(), release_conn=Mock()))
    mock.stat_object = MagicMock()
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

def test_update_item_access_removes_from_protected(db_session, mock_minio):
    """Test removing item from protected bucket when status changes from borrowable."""
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
    
    item.item_status = "open_access"
    db_session.commit()
    db_session.refresh(item)

    mock_minio.remove_object.return_value = None
    result = update_item_access(db_session, "book1") 
    
    assert result is True
    updated_item = db_session.query(Item).filter(Item.identifier == "book1").first()
    assert updated_item.s3_protected_path is None 
    mock_minio.remove_object.assert_called_once_with("lenny-protected", "book1.pdf")
    mock_minio.put_object.assert_not_called()

def test_update_item_access_adds_to_protected(db_session, mock_minio):
    """Test adding item to protected bucket when status changes to borrowable."""
    item = Item(
        identifier="book1", 
        title="Test Book", 
        item_status="open_access", 
        language="en", 
        num_lendable_total=1, 
        current_num_lendable=1, 
        s3_public_path="s3://lenny-public/book1.pdf",
        s3_protected_path=None 
    )
    db_session.add(item)
    db_session.commit()

    item.item_status = "borrowable"
    db_session.commit()
    db_session.refresh(item)

    mock_minio.put_object.reset_mock()
    mock_minio.get_object.reset_mock()
    mock_minio.stat_object.reset_mock()
    mock_minio.remove_object.reset_mock()

    stat_mock_public = Mock()
    stat_mock_public.size = 1024

    def stat_side_effect(bucket_name, object_name):
        if bucket_name == S3_CONFIG["protected_bucket"]:
            raise S3Error(
                code='NoSuchKey',
                message='Not found',
                resource=f'/{bucket_name}/{object_name}',
                request_id='dummy_request_id',
                host_id='dummy_host_id',
                response=Mock(status=404) 
            )
        elif bucket_name == S3_CONFIG["public_bucket"]:
            return stat_mock_public
        else:
            raise ValueError(f"Unexpected bucket in stat_object mock: {bucket_name}")

    mock_minio.stat_object.side_effect = stat_side_effect

    result = update_item_access(db_session, "book1")

    assert result is True
    updated_item = db_session.query(Item).filter(Item.identifier == "book1").first()
    assert updated_item.s3_protected_path == f"s3://{S3_CONFIG['protected_bucket']}/book1.pdf" 
    mock_minio.put_object.assert_called_once()
    mock_minio.get_object.assert_called_once_with(S3_CONFIG["public_bucket"], "book1.pdf")
    assert mock_minio.stat_object.call_count == 2
    mock_minio.remove_object.assert_not_called()

def test_update_item_access_borrowable_already_protected(db_session, mock_minio):
    """Test no action taken if item is borrowable and already in protected bucket."""
    item = Item(
        identifier="book1", 
        title="Test Book", 
        item_status="borrowable", 
        language="en", 
        num_lendable_total=1,
        current_num_lendable=1,
        current_waitlist_size=0,
        s3_public_path="s3://lenny-public/book1.pdf",
        s3_protected_path="s3://lenny-protected/book1.pdf" 
    )
    db_session.add(item)
    db_session.commit()

    result = update_item_access(db_session, "book1")

    assert result is True
    updated_item = db_session.query(Item).filter(Item.identifier == "book1").first()
    assert updated_item.s3_protected_path == "s3://lenny-protected/book1.pdf" 
    mock_minio.put_object.assert_not_called()
    mock_minio.remove_object.assert_not_called()

def test_update_item_access_not_borrowable_not_protected(db_session, mock_minio):
    """Test no action taken if item is not borrowable and not in protected bucket."""
    item = Item(
        identifier="book1", 
        title="Test Book", 
        item_status="open_access", 
        language="en", 
        num_lendable_total=1,
        current_num_lendable=1,
        current_waitlist_size=0,
        s3_public_path="s3://lenny-public/book1.pdf",
        s3_protected_path=None 
    )
    db_session.add(item)
    db_session.commit()


    mock_minio.put_object.reset_mock()
    mock_minio.remove_object.reset_mock()
    mock_minio.stat_object.reset_mock()

    def raise_no_such_key(bucket_name, object_name):
        raise S3Error(
            code='NoSuchKey',
            message='Not found',
            resource=f'/{bucket_name}/{object_name}', 
            request_id='dummy_request_id',
            host_id='dummy_host_id',
            response=Mock(status=404) 
        )
    mock_minio.stat_object.side_effect = raise_no_such_key

    result = update_item_access(db_session, "book1")

    assert result is True
    updated_item = db_session.query(Item).filter(Item.identifier == "book1").first()
    assert updated_item.s3_protected_path is None
    mock_minio.put_object.assert_not_called()
    mock_minio.remove_object.assert_not_called()
    mock_minio.stat_object.assert_called_once_with(S3_CONFIG["protected_bucket"], "book1.pdf")

# --- Tests for Metadata Extraction ---

@patch('lenny.core.items.epub.read_epub')
def test_extract_epub_metadata_success(mock_read_epub):
    mock_book = Mock()
    mock_book.get_metadata.side_effect = lambda ns, name: {
        ('DC', 'identifier'): [('urn:uuid:12345', {})],
        ('DC', 'title'): [('Test EPUB Title', {})],
        ('DC', 'language'): [('fr-FR', {})],
    }.get((ns, name), [])
    mock_read_epub.return_value = mock_book

    metadata = extract_epub_metadata("fake/path/book.epub")

    assert metadata["identifier"] == "urn:uuid:12345"
    assert metadata["title"] == "Test EPUB Title"
    assert metadata["language"] == "fr"
    mock_read_epub.assert_called_once_with("fake/path/book.epub", options={'ignore_ncx': True})

@patch('lenny.core.items.epub.read_epub')
def test_extract_epub_metadata_fallback(mock_read_epub):
    mock_book = Mock()
    mock_book.get_metadata.side_effect = lambda ns, name: {
        ('DC', 'language'): [('en', {})],
    }.get((ns, name), [])
    mock_read_epub.return_value = mock_book

    metadata = extract_epub_metadata("fake/path/book_no_meta.epub")

    assert metadata["identifier"] == "book_no_meta"
    assert metadata["title"] == "book_no_meta"
    assert metadata["language"] == "en"
    mock_read_epub.assert_called_once_with("fake/path/book_no_meta.epub", options={'ignore_ncx': True})

@patch('lenny.core.items.PdfReader')
def test_extract_pdf_metadata_success(mock_pdf_reader):
    mock_meta = Mock()
    mock_meta.title = "Test PDF Title"
    mock_instance = Mock()
    mock_instance.metadata = mock_meta
    mock_pdf_reader.return_value = mock_instance

    metadata = extract_pdf_metadata("fake/path/book.pdf")

    assert metadata["identifier"] == "book" 
    assert metadata["title"] == "Test PDF Title"
    assert metadata["language"] == "en" 
    mock_pdf_reader.assert_called_once_with("fake/path/book.pdf")

@patch('lenny.core.items.PdfReader') 
def test_extract_pdf_metadata_fallback(mock_pdf_reader):
    mock_meta = Mock()
    mock_meta.title = None 
    mock_instance = Mock()
    mock_instance.metadata = mock_meta
    mock_pdf_reader.return_value = mock_instance

    metadata = extract_pdf_metadata("fake/path/book_no_title.pdf")

    assert metadata["identifier"] == "book_no_title" 
    assert metadata["title"] == "book_no_title" 
    assert metadata["language"] == "en" 
    mock_pdf_reader.assert_called_once_with("fake/path/book_no_title.pdf")


# --- Tests for process_book_file ---

@patch('lenny.core.items.extract_epub_metadata')
@patch('lenny.core.items.upload_item')
@patch('os.path.exists', return_value=True)
def test_process_book_file_epub_new(mock_path_exists, mock_upload, mock_extract, db_session):
    mock_extract.return_value = {
        "identifier": "epub 123",
        "title": "Test EPUB Title",
        "language": "en",
    }
    mock_upload.return_value = Item(identifier="Test_EPUB_Title", title="Test EPUB Title")
    mock_session = MagicMock(spec=Session)
    mock_session.query.return_value.filter.return_value.first.return_value = None

    result = process_book_file(mock_session, "fake/book.epub")

    mock_path_exists.assert_called_once_with("fake/book.epub")
    mock_extract.assert_called_once_with("fake/book.epub")
    mock_session.query.return_value.filter.assert_called_once()
    mock_upload.assert_called_once()
    args, kwargs = mock_upload.call_args
    assert kwargs['session'] == mock_session
    assert kwargs['identifier'] == "Test_EPUB_Title"
    assert kwargs['title'] == "Test EPUB Title"
    assert kwargs['language'] == "en"
    assert kwargs['file_path'] == "fake/book.epub"
    assert kwargs['item_status'] == "available"
    assert kwargs['is_lendable'] is True
    assert kwargs['num_lendable_total'] == 1
    assert result[0] is not None
    assert result[0].identifier == "Test_EPUB_Title"
    assert result[1] == "created"

@patch('lenny.core.items.extract_pdf_metadata')
@patch('lenny.core.items.upload_item')
@patch('os.path.exists', return_value=True)
def test_process_book_file_pdf_new(mock_path_exists, mock_upload, mock_extract, db_session):
    mock_extract.return_value = {
        "identifier": "pdf 456",
        "title": "PDF Test",
        "language": "de",
    }
    mock_upload.return_value = Item(identifier="PDF_Test", title="PDF Test")
    mock_session = MagicMock(spec=Session)
    mock_session.query.return_value.filter.return_value.first.return_value = None

    result = process_book_file(mock_session, "fake/book.pdf")

    mock_path_exists.assert_called_once_with("fake/book.pdf")
    mock_extract.assert_called_once_with("fake/book.pdf")
    mock_session.query.return_value.filter.assert_called_once()
    mock_upload.assert_called_once()
    args, kwargs = mock_upload.call_args
    assert kwargs['identifier'] == "PDF_Test"
    assert kwargs['language'] == "de"
    assert result[0] is not None
    assert result[0].identifier == "PDF_Test"
    assert result[1] == "created"

@patch('lenny.core.items.extract_epub_metadata')
@patch('lenny.core.items.upload_item')
@patch('os.path.exists', return_value=True)
def test_process_book_file_existing(mock_path_exists, mock_upload, mock_extract, db_session):
    mock_extract.return_value = {"identifier": "epub 123", "title": "Test EPUB Title", "language": "en"}
    existing_item = Item(identifier="Test_EPUB_Title", title="Existing EPUB")
    mock_session = MagicMock(spec=Session)
    mock_session.query.return_value.filter.return_value.first.return_value = existing_item

    result = process_book_file(mock_session, "fake/book.epub")

    mock_path_exists.assert_called_once_with("fake/book.epub")
    mock_extract.assert_called_once_with("fake/book.epub")
    mock_session.query.return_value.filter.assert_called_once()
    mock_upload.assert_not_called()
    assert result[0] == existing_item
    assert result[1] == "existing"

@patch('lenny.core.items.upload_item')
@patch('os.path.exists', return_value=True)
def test_process_book_file_unsupported(mock_path_exists, mock_upload, db_session):
    mock_session = MagicMock(spec=Session)
    result = process_book_file(mock_session, "fake/book.txt")
    mock_path_exists.assert_called_once_with("fake/book.txt")
    mock_upload.assert_not_called()
    assert result[0] is None
    assert result[1] == "skipped_unsupported"

@patch('lenny.core.items.extract_pdf_metadata')
@patch('lenny.core.items.upload_item', side_effect=Exception("Upload failed"))
@patch('os.path.exists', return_value=True)
def test_process_book_file_upload_error(mock_path_exists, mock_upload, mock_extract, db_session):
    mock_extract.return_value = {"identifier": "pdf456", "title": "PDF Test", "language": "de"}
    mock_session = MagicMock(spec=Session)
    mock_session.query.return_value.filter.return_value.first.return_value = None

    result = process_book_file(mock_session, "fake/book.pdf")

    mock_path_exists.assert_called_once_with("fake/book.pdf")
    mock_extract.assert_called_once_with("fake/book.pdf")
    mock_session.query.return_value.filter.assert_called_once()
    mock_upload.assert_called_once()
    assert result[0] is None
    assert result[1].startswith("error_upload_db")