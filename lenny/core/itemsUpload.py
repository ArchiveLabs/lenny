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

from lenny.models import db
from lenny.models.items import Item  
from lenny.core import s3 

def upload_items(openlibrary_edition: int, encrypted: bool, files: list[UploadFile], db_session: Session = db):

    bucket_name = "bookshelf-encrypted" if encrypted else "bookshelf-public"

    for file_upload in files:
        if not file_upload.filename:
            continue 
        file_extension = Path(file_upload.filename).suffix.lower()
        s3_object_name = f"{openlibrary_edition}{file_extension}"
        
        try:
            file_upload.file.seek(0)
            extra_args = {'ContentType': file_upload.content_type}
            
            s3.upload_fileobj(
                file_upload.file, 
                bucket_name,
                s3_object_name,
                ExtraArgs=extra_args
            )
            s3_filepath = f"{bucket_name}/{s3_object_name}"

            new_item = Item(
                openlibrary_edition=openlibrary_edition,
                encrypted=encrypted,
                s3_filepath=s3_filepath
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
