from sqlalchemy.orm import Session
from minio import Minio, error as minio_error
from lenny.models.items import Item
from lenny.configs import S3_CONFIG
import os
import shutil
from typing import Optional

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
        # Check if public bucket exists and create it if it doesn't
        if not minio_client.bucket_exists(public_bucket):
            minio_client.make_bucket(public_bucket)
            print(f"Created MinIO bucket: {public_bucket}")
            
        # Check if protected bucket exists and create it if it doesn't
        if not minio_client.bucket_exists(protected_bucket):
            minio_client.make_bucket(protected_bucket)
            print(f"Created MinIO bucket: {protected_bucket}")
            
        return True
    except minio_error.S3Error as e:
        print(f"Error initializing MinIO buckets: {str(e)}")
        return False

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
    num_lendable_total: int = 0,
    current_num_lendable: int = 0,
    current_waitlist_size: int = 0,
) -> Optional[Item]:
    # Ensure buckets exist before upload
    initialize_minio_buckets()
    
    # Extract file extension
    _, file_extension = os.path.splitext(file_path)
    
    # Create object name with extension
    object_name = f"{identifier}{file_extension}"
    
    minio_client = Minio(
        endpoint=S3_CONFIG["endpoint"],
        access_key=S3_CONFIG["access_key"],
        secret_key=S3_CONFIG["secret_key"],
        secure=S3_CONFIG["secure"],
    )

    # Upload to public bucket
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
        raise Exception(f"MinIO public upload error: {str(e)}")

    s3_protected_path = None
    if item_status.lower() == "borrowable":
        protected_bucket = S3_CONFIG["protected_bucket"]
        try:
            response = minio_client.get_object(public_bucket, object_name)
            minio_client.put_object(
                protected_bucket,
                object_name,
                response,
                length=os.path.getsize(file_path),
            )
            s3_protected_path = f"s3://{protected_bucket}/{object_name}"
            response.close()
            response.release_conn()
        except minio_error.S3Error as e:
            raise Exception(f"MinIO protected copy error: {str(e)}")

    # Store the file extension in the database record
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
    # Ensure buckets exist before delete operation
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

    # Extract the object name from the s3 path
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
    # Ensure buckets exist before update operation
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

    # Extract the object name from the s3 paths
    if not item.s3_public_path:
        # Cannot proceed if there's no public path
        print(f"Warning: Item {identifier} has no s3_public_path. Cannot update access.")
        return False
    object_name = item.s3_public_path.split("/")[-1]
    protected_bucket = S3_CONFIG["protected_bucket"]

    is_borrowable = item.item_status.lower() == "borrowable"
    is_in_protected = item.s3_protected_path is not None

    try:
        if is_borrowable and not is_in_protected:
            # Copy from public to protected
            print(f"Item {identifier} is borrowable, copying to protected bucket.")
            response = minio_client.get_object(S3_CONFIG["public_bucket"], object_name)
            stats = minio_client.stat_object(S3_CONFIG["public_bucket"], object_name)
            file_size = stats.size if stats else 0
            
            minio_client.put_object(
                protected_bucket,
                object_name,
                response,
                length=file_size,
            )
            item.s3_protected_path = f"s3://{protected_bucket}/{object_name}"
            session.commit()
            response.close()
            response.release_conn()
            print(f"Item {identifier} copied to protected bucket.")

        elif not is_borrowable and is_in_protected:
            # Remove from protected
            print(f"Item {identifier} is not borrowable, removing from protected bucket.")
            protected_object_name = item.s3_protected_path.split("/")[-1]
            minio_client.remove_object(protected_bucket, protected_object_name)
            item.s3_protected_path = None
            session.commit()
            print(f"Item {identifier} removed from protected bucket.")
        else:
            # No change needed
            print(f"Item {identifier} access status already consistent. Borrowable: {is_borrowable}, In Protected: {is_in_protected}")
            pass 

    except minio_error.S3Error as e:
        session.rollback() # Rollback DB changes on S3 error
        print(f"MinIO error during access update for {identifier}: {str(e)}")
        raise Exception(f"MinIO error during access update: {str(e)}")
    except Exception as e:
        session.rollback()
        print(f"General error during access update for {identifier}: {str(e)}")
        raise # Re-raise other exceptions

    return True