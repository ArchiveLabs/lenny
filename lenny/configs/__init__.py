#!/usr/bin/env python

"""
    Configurations for Lenny

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import os


# Determine environment
TESTING = os.getenv("TESTING", "false").lower() == "true"

# API server configuration
DOMAIN = os.environ.get('LENNY_DOMAIN', '127.0.0.1')
HOST = os.environ.get('LENNY_HOST', '0.0.0.0')
PORT = int(os.environ.get('LENNY_PORT', 8080))
WORKERS = int(os.environ.get('LENNY_WORKERS', 1))
DEBUG = bool(int(os.environ.get('LENNY_DEBUG', 0)))

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

DB_CONFIG = {
    'user': os.environ.get('DB_USER', 'postgres'),
    'password': os.environ.get('DB_PASSWORD'),
    'host': os.environ.get('DB_HOST', 'localhost'),
    'port': int(os.environ.get('DB_PORT', '5432')),
    'dbname': os.environ.get('DB_NAME', 'lenny'),
}

# Database configuration
DB_URI = (
    "sqlite:///:memory:" if TESTING else
    'postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}'.format(**DB_CONFIG)
)            

# MinIO configuration
S3_CONFIG = {
    'endpoint': os.environ.get('S3_ENDPOINT'),
    'access_key': os.environ.get('MINIO_ROOT_USER'),
    'secret_key': os.environ.get('MINIO_ROOT_PASSWORD'),
    'secure': os.environ.get('S3_SECURE', 'false').lower() == 'true',
}

__all__ = ['DOMAIN', 'HOST', 'PORT', 'DEBUG', 'OPTIONS', 'DB_URI', 'DB_CONFIG','S3_CONFIG', 'TESTING']
