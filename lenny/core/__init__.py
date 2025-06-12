#!/usr/bin/env python

"""
    Core module for Lenny,
    including the main application setup and configuration.
    
    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import boto3
from lenny.configs import S3_CONFIG


# Initialize S3 client for MinIO
session = boto3.session.Session()

s3 = session.client(
    service_name='s3',
    aws_access_key_id=S3_CONFIG['access_key'],
    aws_secret_access_key=S3_CONFIG['secret_key'],
    endpoint_url=f"http://{S3_CONFIG['endpoint']}",
    use_ssl=S3_CONFIG['secure']
)

# Define bucket names
BUCKET_NAMES = ["bookshelf"]

# Create buckets if they don't exist
for bucket_name in BUCKET_NAMES:
    try:
        # Check if the bucket already exists
        s3.head_bucket(Bucket=bucket_name)
        print(f"Bucket '{bucket_name}' already exists.")
    except Exception as e:
        # If head_bucket throws an exception (e.g., NoSuchBucket), the bucket doesn't exist
        try:
            s3.create_bucket(Bucket=bucket_name)
            print(f"Bucket '{bucket_name}' created successfully.")
        except Exception as create_error:
            print(f"Error creating bucket '{bucket_name}': {create_error}")

def get_bookshelf_keys(prefix=''):
    """
    Lists all object keys (filenames) in a specified S3 bucket,
    optionally filtered by a prefix. Handles pagination automatically.
    """
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket='bookshelf', Prefix=prefix)
    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                yield obj['Key']

s3.get_bookshelf_keys = get_bookshelf_keys
                
__all__ = ['s3']

