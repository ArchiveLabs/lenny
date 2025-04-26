#!/usr/bin/env python

"""
    Configurations for Lenny

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env.test if it exists
load_dotenv(dotenv_path=".env.test", override=True)

# Determine environment
TESTING = os.getenv("TESTING", "False").lower() == "true"

# API server configuration
DOMAIN = os.environ.get('LENNY_DOMAIN', '127.0.0.1')
HOST = os.environ.get('LENNY_HOST', '0.0.0.0')
PORT = int(os.environ.get('LENNY_PORT', 8080))
WORKERS = int(os.environ.get('LENNY_WORKERS', 1))
DEBUG = bool(int(os.environ.get('LENNY_DEBUG', 0)))  # Disabled by default in production

LOG_LEVEL = os.environ.get('LENNY_LOG_LEVEL', 'info')
SSL_CRT = os.environ.get('LENNY_SSL_CRT')
SSL_KEY = os.environ.get('LENNY_SSL_KEY')

OPTIONS = {
    'host': HOST,
    'port': PORT,
    'log_level': LOG_LEVEL,
    'reload': DEBUG,
    'workers': WORKERS,
}
if SSL_CRT and SSL_KEY:
    OPTIONS['ssl_keyfile'] = SSL_KEY
    OPTIONS['ssl_certfile'] = SSL_CRT

# Database configuration
if TESTING:
    DB_URI = "sqlite:///:memory:"
else:
    DB_CONFIG = {
        'user': os.environ.get('POSTGRES_USER', 'lenny'),
        'password': os.environ.get('POSTGRES_PASSWORD', 'lennytest'),
        'host': os.environ.get('POSTGRES_HOST', 'postgres'),
        'port': int(os.environ.get('POSTGRES_PORT', '5432')),
        'dbname': os.environ.get('POSTGRES_DB', 'lending_system'),
    }
    DB_URI = 'postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}'.format(**DB_CONFIG)

# MinIO configuration
S3_CONFIG = {
    'access_key': os.environ.get('S3_ACCESS_KEY', os.environ.get('MINIO_ROOT_USER')),
    'secret_key': os.environ.get('S3_SECRET_KEY', os.environ.get('MINIO_ROOT_PASSWORD')),
    'endpoint': f"{os.environ.get('MINIO_HOST', 'minio')}:{os.environ.get('MINIO_PORT', '9000')}",
    'secure': False,
    'public_bucket': os.environ.get('MINIO_BUCKET', 'lenny') + "-public",
    'protected_bucket': os.environ.get('MINIO_BUCKET', 'lenny') + "-protected",
}

__all__ = ['DOMAIN', 'HOST', 'PORT', 'DEBUG', 'OPTIONS', 'DB_URI', 'S3_CONFIG', 'TESTING']