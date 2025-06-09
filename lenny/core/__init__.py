#!/usr/bin/env python

"""
    Core module for Lenny,
    including the main application setup and configuration.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import boto3
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base 
from lenny.models import items
from lenny.configs import DB_URI, DEBUG, S3_CONFIG


class LennyDB:

    def __init__(self):
        self.engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
        self.db = scoped_session(sessionmaker(bind=self.engine, autocommit=False, autoflush=False))
        self.Base = declarative_base()
        self._initialize()

    def __getattr__(self, name):
        # Delegate any unknown attribute or method to the db session
        return getattr(self.db, name)
        
    def _initialize(self):
        """Initializes the database and creates tables."""
        try:
            self.Base.metadata.create_all(bind=self.engine)
        except Exception as e:
            print(f"[WARNING] Database initialization failed: {e}")

class LennyS3:

    BOOKSHELF_BUCKET = "bookshelf"
    
    def __init__(self):
        # Initialize S3 client for MinIO
        s3 = boto3.session.Session().client(
            service_name='s3',
            aws_access_key_id=S3_CONFIG['access_key'],
            aws_secret_access_key=S3_CONFIG['secret_key'],
            endpoint_url=f"http://{S3_CONFIG['endpoint']}",
            use_ssl=S3_CONFIG['secure']
        )
        self._initialize()

    def __getattr__(self, name):
        # Delegate any unknown attribute or method to the boto3 s3 client
        return getattr(self.s3, name)

    def _initialize(self):
        try:
            self.s3.head_bucket(Bucket=self.BOOKSHELF_BUCKET)
            print(f"Bucket '{self.BOOKSHELF_BUCKET}' already exists.")
        except Exception as e:
            try:
                self.s3.create_bucket(Bucket=self.BOOKSHELF_BUCKET)
                print(f"Bucket '{self.BOOKSHELF_BUCKET}' created successfully.")
            except Exception as create_error:
                print(f"Error creating bucket '{self.BOOKSHELF_BUCKET}': {create_error}")

    def get_keys(self, prefix=''):
        """
        Lists all object keys (filenames) in a specified S3 bucket,
        optionally filtered by a prefix. Handles pagination automatically.

        Args:
            bucket (str): The name of the S3 bucket.
            prefix (str): An optional prefix to filter the results.

        Returns:
            list: A list of strings, where each string is an object key.
        """
        paginator = self.s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=self.BOOKSHELF_BUCKET, Prefix=prefix)

        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    yield obj['Key']

db = LennyDB()
s3 = LennyS3()


__all__ = ["db", "s3"]
