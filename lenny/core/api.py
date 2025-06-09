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

from lenny.models.items import Item, FormatEnum
from lenny.core import db, s3

class LennyAPI:

    VALID_EXTS = {
        ".pdf": FormatEnum.PDF,
        ".epub": FormatEnum.EPUB
    }

    @classmethod
    def encrypt_file(cls, f, method="lcp"):
        # XXX Not Implemented
        return f

    @classmethod
    def upload_file(cls, fp):
        if file.size and file.size <= s3.MAX_FILE_SIZE:
            fp.file.seek(0)
            s3.upload_fileobj(
                fp.file,
                s3.BOOKSHELF_BUCKET,
                filename,
                ExtraArgs={'ContentType': fp.content_type}
            )
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{file.filename} exceeds {s3.MAX_FILE_SIZE // (1024 * 1024)}MB."
        )

    def upload_files(cls, files: list[UploadFile], filename, encrypt=False):
        formats = 0
        for fp in files:
            if not fp.filename:
                continue

            ext = Path(fp.filename).suffix.lower()

            try:
                fmt: FormatEnum = cls.VALID_EXTS[ext]:
                formats += fmt
            except Exception:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unsupported file format: '{ext}' for file '{fp.filename}'. Only {','.join(cls.VALID_EXTS)} supported."
                )

            # Upload the unencrypted file to s3
            cls.upload_file(fp, f"{olid}{ext}")
            if encrypt:
                cls.upload_file(cls.encrypt_file(fp), f"{olid}_encrypted{ext}")

        return formats

    @classmethod
    def add(cls, openlibrary_edition: int, files: list[UploadFile], encrypt: bool=False):
        if Item.exists(openlibrary_edition):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Item with OpenLibrary Edition ID '{openlibrary_edition}' already exists."
            )
        try:
            formats = cls.upload_files(files, openlibrary_edition, encrypt=encrypt)
            if not formats:
                raise Exception("No valid files provided")
            item = Item(
                openlibrary_edition=openlibrary_edition,
                encrypted=encrypt,
                formats=FormatEnum(formats)
            )
            db_session.add(itemitem)

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
