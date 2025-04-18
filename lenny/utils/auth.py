#!/usr/bin/env python
from fastapi import HTTPException, Depends, Request
from lenny.configs import S3_CONFIG
import os

def s3librarianauthcheck(s3_access_key: str = None, s3_secret_key: str = None):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            expected_access_key = os.environ.get('MINIO_ROOT_USER')
            expected_secret_key = os.environ.get('MINIO_ROOT_PASSWORD')
            if not (s3_access_key == expected_access_key and s3_secret_key == expected_secret_key):
                raise HTTPException(status_code=401, detail="Invalid S3 credentials")
            return await func(*args, **kwargs)
        return wrapper
    return decorator