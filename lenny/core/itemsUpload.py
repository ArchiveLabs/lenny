#!/usr/bin/env python

"""
    Items Upload Module for Lenny,
    including the upload functionality for items to the database and MinIO storage.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""
import os
from pathlib import Path
from fastapi import UploadFile, HTTPException, status
from sqlalchemy.orm import Session
from botocore.exceptions import ClientError 

from lenny.core.models import FormatEnum, Item  
from lenny.core import s3, db

def upload_items(openlibrary_edition: int, encrypted: bool, files: list[UploadFile], db_session: Session = db):
    bucket_name = "bookshelf" 
        
    for file_upload in files:
        if not file_upload.filename:
            continue 
        file_extension = Path(file_upload.filename).suffix.lower()    
        if file_extension == ".pdf":
            formats_value = "PDF"
        elif file_extension == ".epub":
            formats_value = "EPUB"
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported file format: '{file_extension}' for file '{file_upload.filename}'. Only '.pdf' and '.epub' are supported."
            )
        if encrypted:
            s3_object_name = f"{openlibrary_edition}_encrypted{file_extension}"
        else:
            s3_object_name = f"{openlibrary_edition}{file_extension}"
        
        try:
            file_upload.file.seek(0)
            extra_args = {'ContentType': file_upload.headers.get("content-type", "application/octet-stream")}
            s3.upload_fileobj(
                file_upload.file, 
                bucket_name,
                s3_object_name,
                ExtraArgs=extra_args
            )

            new_item = Item(
                openlibrary_edition=openlibrary_edition,
                encrypted=encrypted,
                formats=formats_value,
            )
            db_session.add(new_item)
        
        except ClientError as e: 
            db_session.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to upload '{file_upload.filename}' to S3: {e.response.get('Error', {}).get('Message', str(e))}.")
        except Exception as e: 
            db_session.rollback()
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"An error occurred with file \'{file_upload.filename}\': {str(e)}.")

    try:
        db_session.commit()
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Database commit failed: {str(e)}.")
