#!/usr/bin/env python

"""
    Core module for Lenny, s3 & db
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import boto3
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker, declarative_base 
from lenny.configs import DB_URI, DEBUG, S3_CONFIG

Base = declarative_base()

# Import all models here to ensure they are registered with Base
from lenny.core.models import Item

# Configure Database Connection
engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
db = scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False))

def init_db(engine_to_init=engine):
    """Initializes the database and creates tables."""
    Base.metadata.create_all(bind=engine_to_init)

def _auto_init_db():
    try:
        init_db(engine)
    except Exception as e:
        print(f"[WARNING] Database initialization failed: {e}")

_auto_init_db()

class LennyS3:

    BOOKSHELF_BUCKET = "bookshelf"
    
    def __init__(self):
        # Initialize S3 client for MinIO
        self.s3 = boto3.session.Session().client(
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

    def get_keys(self, bucket=None, prefix=''):
        """
        Lists all object keys (filenames) in a specified S3 bucket,
        optionally filtered by a prefix. Handles pagination automatically.
        """
        bucket=bucket or self.BOOKSHELF_BUCKET
        paginator = self.s3.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)

        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    yield obj['Key']

s3 = LennyS3()
                

__all__ = ["s3", "Base", "db", "engine", "items", "init_db"]
