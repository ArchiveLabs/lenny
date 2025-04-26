from sqlalchemy.orm import Session
from minio import Minio, error as minio_error
from lenny.models.items import Item
from lenny.configs import S3_CONFIG
import os
import shutil
from typing import Optional, Tuple
from ebooklib import epub
from pypdf import PdfReader
from concurrent.futures import ThreadPoolExecutor, as_completed
import re  
import json

def initialize_minio_buckets():
    """Initialize MinIO buckets if they don't exist."""
    minio_client = Minio(
        endpoint=S3_CONFIG["endpoint"],
        access_key=S3_CONFIG["access_key"],
        secret_key=S3_CONFIG["secret_key"],
        secure=S3_CONFIG["secure"],
    )
    
    public_bucket = S3_CONFIG["public_bucket"]
    protected_bucket = S3_CONFIG["protected_bucket"]
    
    try:
        if not minio_client.bucket_exists(public_bucket):
            minio_client.make_bucket(public_bucket)
            policy = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": ["*"]},
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{public_bucket}/*"]
                    }
                ]
            }
            minio_client.set_bucket_policy(public_bucket, json.dumps(policy))

        if not minio_client.bucket_exists(protected_bucket):
            minio_client.make_bucket(protected_bucket)

    except minio_error.S3Error as e:
        raise RuntimeError(f"Failed to initialize MinIO buckets: {e}") from e

def upload_item(
    session: Session,
    identifier: str,  
    title: str,
    item_status: str,
    language: str,
    file_path: str,
    is_readable: bool = False,
    is_lendable: bool = True,
    is_waitlistable: bool = True,
    is_printdisabled: bool = False,
    is_login_required: bool = False,
    num_lendable_total: int = 1,
    current_num_lendable: int = 0,
    current_waitlist_size: int = 0,
) -> Optional[Item]:
    initialize_minio_buckets()

    _, file_extension = os.path.splitext(file_path)

    # Sanitize the final identifier for use in S3 object name
    safe_s3_identifier = re.sub(r'[\\s/:]+', '_', identifier)

    # Use the sanitized identifier for the object name
    object_name = f"{safe_s3_identifier}{file_extension}"

    minio_client = Minio(
        endpoint=S3_CONFIG["endpoint"],
        access_key=S3_CONFIG["access_key"],
        secret_key=S3_CONFIG["secret_key"],
        secure=S3_CONFIG["secure"],
    )

    public_bucket = S3_CONFIG["public_bucket"]
    try:
        with open(file_path, "rb") as file_data:
            minio_client.put_object(
                public_bucket,
                object_name,
                file_data,
                length=os.path.getsize(file_path),
                metadata={"browsable": "false"},
            )
        s3_public_path = f"s3://{public_bucket}/{object_name}"
    except minio_error.S3Error as e:
        raise Exception(f"MinIO public upload error for object '{object_name}': {str(e)}")

    s3_protected_path = None
    if item_status.lower() == "borrowable":
        protected_bucket = S3_CONFIG["protected_bucket"]
        try:
            response = minio_client.get_object(public_bucket, object_name)
            stats = minio_client.stat_object(public_bucket, object_name)
            minio_client.put_object(
                protected_bucket,
                object_name,
                response,
                length=stats.size,
            )
            s3_protected_path = f"s3://{protected_bucket}/{object_name}"
            response.close()
            response.release_conn()
        except minio_error.S3Error as e:
            raise Exception(f"MinIO protected copy error for object '{object_name}': {str(e)}")

    item = Item(
        identifier=identifier,
        title=title,
        item_status=item_status,
        language=language,
        is_readable=is_readable,
        is_lendable=is_lendable,
        is_waitlistable=is_waitlistable,
        is_printdisabled=is_printdisabled,
        is_login_required=is_login_required,
        num_lendable_total=num_lendable_total,
        current_num_lendable=current_num_lendable,
        current_waitlist_size=current_waitlist_size,
        s3_public_path=s3_public_path,
        s3_protected_path=s3_protected_path,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item

def delete_item(session: Session, identifier: str) -> bool:
    initialize_minio_buckets()
    
    minio_client = Minio(
        endpoint=S3_CONFIG["endpoint"],
        access_key=S3_CONFIG["access_key"],
        secret_key=S3_CONFIG["secret_key"],
        secure=S3_CONFIG["secure"],
    )

    item = session.query(Item).filter(Item.identifier == identifier).first()
    if not item:
        return False

    object_name = item.s3_public_path.split("/")[-1]
    
    try:
        minio_client.remove_object(S3_CONFIG["public_bucket"], object_name)
    except minio_error.S3Error as e:
        raise Exception(f"MinIO public delete error: {str(e)}")

    if item.s3_protected_path:
        protected_object_name = item.s3_protected_path.split("/")[-1]
        try:
            minio_client.remove_object(S3_CONFIG["protected_bucket"], protected_object_name)
        except minio_error.S3Error as e:
            raise Exception(f"MinIO protected delete error: {str(e)}")

    session.delete(item)
    session.commit()
    return True

def update_item_access(session: Session, identifier: str) -> bool:
    """Synchronizes the item's presence in the protected S3 bucket based on its item_status."""
    initialize_minio_buckets()

    minio_client = Minio(
        endpoint=S3_CONFIG["endpoint"],
        access_key=S3_CONFIG["access_key"],
        secret_key=S3_CONFIG["secret_key"],
        secure=S3_CONFIG["secure"],
    )

    item = session.query(Item).filter(Item.identifier == identifier).first()
    if not item:
        return False

    if not item.s3_public_path:
        return False
    object_name = item.s3_public_path.split("/")[-1]
    protected_bucket = S3_CONFIG["protected_bucket"]

    is_borrowable = item.item_status.lower() == "borrowable"
    object_in_protected_bucket = False
    try:
        minio_client.stat_object(protected_bucket, object_name)
        object_in_protected_bucket = True
    except minio_error.S3Error as err:
        if err.code == 'NoSuchKey':
            object_in_protected_bucket = False
        else:
            raise Exception(f"MinIO error checking protected object '{object_name}': {str(err)}")

    try:
        if is_borrowable and not object_in_protected_bucket:
            response = minio_client.get_object(S3_CONFIG["public_bucket"], object_name)
            stats = minio_client.stat_object(S3_CONFIG["public_bucket"], object_name)
            minio_client.put_object(
                protected_bucket,
                object_name,
                response,
                length=stats.size,
            )
            item.s3_protected_path = f"s3://{protected_bucket}/{object_name}"
            session.commit()
            response.close()
            response.release_conn()

        elif not is_borrowable and object_in_protected_bucket:
            minio_client.remove_object(protected_bucket, object_name)
            item.s3_protected_path = None
            session.commit()
        else:
            pass 

    except minio_error.S3Error as e:
        session.rollback() 
        raise Exception(f"MinIO error during access update: {str(e)}")
    except Exception as e:
        session.rollback()
        raise

    return True

def extract_epub_metadata(file_path: str) -> dict:
    """Extracts metadata (identifier, title, language) from an EPUB file."""
    metadata = {
        "identifier": None,
        "title": None,
        "language": "en",
    }
    try:
        book = epub.read_epub(file_path, options={'ignore_ncx': True})
        identifier_meta = book.get_metadata('DC', 'identifier')
        if identifier_meta:
            metadata["identifier"] = identifier_meta[0][0]

        title_meta = book.get_metadata('DC', 'title')
        if title_meta:
            metadata["title"] = title_meta[0][0]

        language_meta = book.get_metadata('DC', 'language')
        if language_meta:
            lang_code = language_meta[0][0].split('-')[0].lower()
            metadata["language"] = lang_code

    except Exception as e:
        pass

    filename_base = os.path.splitext(os.path.basename(file_path))[0]
    if not metadata["identifier"]:
        metadata["identifier"] = filename_base
    if not metadata["title"]:
        metadata["title"] = filename_base

    return metadata

def extract_pdf_metadata(file_path: str) -> dict:
    """Extracts metadata (title) from a PDF file. Uses filename for identifier."""
    metadata = {
        "identifier": os.path.splitext(os.path.basename(file_path))[0],
        "title": None,
        "language": "en",
    }
    try:
        reader = PdfReader(file_path)
        meta = reader.metadata
        if meta and meta.title:
            metadata["title"] = meta.title
    except Exception as e:
        pass

    if not metadata["title"]:
        metadata["title"] = metadata["identifier"]

    return metadata

def process_book_file(session: Session, file_path: str, original_filename: Optional[str] = None) -> Tuple[Optional[Item], str]:
    """Processes a single book file: extracts metadata, determines identifier, uploads, and returns item and status."""
    if not os.path.exists(file_path):
        return None, "error_not_found"

    base_name_to_sanitize = os.path.basename(original_filename if original_filename else file_path)
    filename_base = os.path.splitext(base_name_to_sanitize.strip())[0]
    sanitized_filename_base = re.sub(r'\s+', '_', filename_base)

    _, file_extension = os.path.splitext(file_path)
    file_extension = file_extension.lower()

    extracted_metadata = {}
    try:
        if file_extension == '.epub':
            extracted_metadata = extract_epub_metadata(file_path)
        elif file_extension == '.pdf':
            extracted_metadata = extract_pdf_metadata(file_path)
        else:
            return None, "skipped_unsupported"
    except Exception as e:
        return None, "error_metadata"

    defaults = {
        "item_status": "available",
        "language": "en",
        "is_readable": False,
        "is_lendable": True,
        "is_waitlistable": True,
        "is_printdisabled": False,
        "is_login_required": False,
        "num_lendable_total": 1,
        "current_num_lendable": 1,
        "current_waitlist_size": 0,
    }

    item_data = {**defaults, **extracted_metadata}

    # --- Identifier and Title Determination ---
    MAX_IDENTIFIER_LEN = 200
    MAX_TITLE_LEN = 255

    title = item_data.get("title")
    identifier_from_metadata = item_data.get("identifier")

    if not title:
        title = sanitized_filename_base.replace('_', ' ')
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN]
    item_data["title"] = title

    identifier = None
    if title:
        identifier = title
    if not identifier and identifier_from_metadata:
        identifier = identifier_from_metadata
    if not identifier:
        identifier = sanitized_filename_base

    # --- Always sanitize the final identifier ---
    identifier = re.sub(r'[\s/:?"<>|*]+', '_', identifier)
    identifier = re.sub(r'_+', '_', identifier)
    identifier = identifier.strip('_')
    if len(identifier) > MAX_IDENTIFIER_LEN:
        identifier = identifier[:MAX_IDENTIFIER_LEN]
    if not identifier:
        return None, "error_identifier_generation_final"
    item_data["identifier"] = identifier

    try:
        existing_item = session.query(Item).filter(Item.identifier == identifier).first()
        if existing_item:
            return existing_item, "existing"
    except Exception as e:
        return None, "error_db_check"

    try:
        item = upload_item(
            session=session,
            identifier=item_data["identifier"],
            title=item_data["title"],
            item_status=item_data["item_status"],
            language=item_data["language"],
            file_path=file_path,
            is_readable=item_data["is_readable"],
            is_lendable=item_data["is_lendable"],
            is_waitlistable=item_data["is_waitlistable"],
            is_printdisabled=item_data["is_printdisabled"],
            is_login_required=item_data["is_login_required"],
            num_lendable_total=item_data["num_lendable_total"],
            current_num_lendable=item_data["current_num_lendable"],
            current_waitlist_size=item_data["current_waitlist_size"],
        )
        if item:
            return item, "created"
        else:
            return None, "error_creation_failed"
    except Exception as e:
        session.rollback()
        return None, f"error_upload_db: {str(e)}"

def batch_process_books(session_factory, file_paths: list[str], max_workers: int = 5) -> dict:
    """
    Processes a list of book files concurrently using ThreadPoolExecutor.
    Uses a session factory to create sessions per thread.
    
    Returns a dictionary with the following structure:
    {
        "success": [{"path": path, "identifier": id, "title": title}],
        "existing": [{"path": path, "identifier": id, "title": title}],
        "failed": [{"path": path, "reason": reason}]
    }
    """
    results = {"success": [], "existing": [], "failed": []}
    
    def process_with_session(file_path):
        with session_factory() as session: 
            try:
                item, status = process_book_file(session, file_path)
                return file_path, item, status
            except Exception as e:
                return file_path, None, f"error_unhandled: {str(e)}"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_with_session, file_path): file_path for file_path in file_paths}
        
        for future in as_completed(futures):
            file_path = futures[future]
            try:
                original_path, item, status = future.result()
                
                if status == "created" and item:
                    results["success"].append({
                        "path": original_path,
                        "identifier": item.identifier,
                        "title": item.title
                    })
                elif status == "existing" and item:
                    results["existing"].append({
                        "path": original_path,
                        "identifier": item.identifier,
                        "title": item.title
                    })
                else:
                    results["failed"].append({
                        "path": original_path,
                        "reason": status
                    })
            except Exception as e:
                results["failed"].append({
                    "path": file_path,
                    "reason": f"Processing error: {str(e)}"
                })

    return results

def preload_books(session_factory):
    """Finds EPUBs and PDFs in Preloaded_books and processes them using batch processing."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    preload_dir = os.path.join(project_root, "Preloaded_books")

    if not os.path.isdir(preload_dir):
        print(f"Preload directory not found: {preload_dir}") 
        return

    supported_extensions = ('.epub', '.pdf')
    book_files = []
    try:
        for f in os.listdir(preload_dir):
            full_path = os.path.join(preload_dir, f)
            if f.lower().endswith(supported_extensions) and os.path.isfile(full_path):
                book_files.append(full_path)
    except OSError as e:
        print(f"Error reading preload directory {preload_dir}: {e}")
        return

    if not book_files:
        print(f"No supported book files found in {preload_dir}") 
        return

    print(f"Found {len(book_files)} books to preload. Processing via batch...") 
    results = batch_process_books(session_factory, book_files)
    print("Preload batch processing complete.") 
    print(f"  Success: {len(results.get('success', []))}")
    print(f"  Existing: {len(results.get('existing', []))}")
    print(f"  Failed: {len(results.get('failed', []))}")
    if results.get('failed'):
        for failed in results['failed']:
            print(f"    - {os.path.basename(failed.get('path', '?'))}: {failed.get('reason', 'Unknown')}")

    return