#!/usr/bin/env python
"""
    Models Configurations for Lenny,
    including handling database connections and ORM setup.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from minio import Minio

from lenny.configs import DB_URI, S3_CONFIG, DEBUG

Base = declarative_base()

# Configure Database Connection
engine = create_engine(DB_URI, echo=DEBUG, client_encoding='utf8')
db = scoped_session(sessionmaker(bind=engine, autocommit=False, autoflush=False))

# Configure S3 Connection
s3 = Minio(
    endpoint=S3_CONFIG["endpoint"],
    access_key=S3_CONFIG["access_key"],
    secret_key=S3_CONFIG["secret_key"],
    secure=S3_CONFIG["secure"],
)

# Instantiate s3 buckets
for bucket_name in ["bookshelf-public", "bookshelf-encrypted"]:
    if not s3.bucket_exists(bucket_name):
        s3.make_bucket(bucket_name)
        # Setting public read-only policy
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{bucket_name}/*"]
                }
            ]
        }
        s3.set_bucket_policy(bucket_name, json.dumps(policy))

__all__ = ["Base", "db", "s3", "engine"]
