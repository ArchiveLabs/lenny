#!/usr/bin/env python

"""
    Configurations for Lenny

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import os

# API server configuration
DOMAIN = os.environ.get('LENNY_DOMAIN', '127.0.0.1')
HOST = os.environ.get('LENNY_HOST', '0.0.0.0')
PORT = int(os.environ.get('LENNY_PORT', 8080))
WORKERS = int(os.environ.get('LENNY_WORKERS', 1))
DEBUG = bool(int(os.environ.get('LENNY_DEBUG', 1)))

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

# Database configuration - prioritize environment variables 
DB_CONFIG = {
    'user': os.environ.get('POSTGRES_USER', 'postgres'),
    'password': os.environ.get('POSTGRES_PASSWORD'),
    'host': os.environ.get('POSTGRES_HOST', 'localhost'),
    'port': int(os.environ.get('POSTGRES_PORT', '5432')),
    'dbname': os.environ.get('POSTGRES_DB', 'lenny'),
}

DB_URI = 'postgres://%(user)s:%(password)s@%(host)s:%(port)s/%(dbname)s' % DB_CONFIG

# MinIO configuration - prioritize environment variables
S3_CONFIG = {
    'user': os.environ.get('MINIO_ROOT_USER'),
    'password': os.environ.get('MINIO_ROOT_PASSWORD'),
    'host': os.environ.get('MINIO_HOST', 'localhost'),
    'port': int(os.environ.get('MINIO_PORT', '9000')),
}

# Export all configuration variables
__all__ = ['config', 'HOST', 'PORT', 'DEBUG', 'OPTIONS', 'DB_CONFIG',
           'S3_CONFIG']
