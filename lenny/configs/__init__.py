#!/usr/bin/env python

"""
    Configurations for Lenny

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import os
from typing import Optional


# Determine environment
TESTING = os.getenv("TESTING", "false").lower() == "true"

# API server configuration
SCHEME = 'http'
PROXY = os.environ.get('LENNY_PROXY', '')
HOST = os.environ.get('LENNY_HOST', 'localhost')
PORT = int(os.environ.get('LENNY_PORT', 8080))
WORKERS = int(os.environ.get('LENNY_WORKERS', 1 if TESTING else 3))
DEBUG = bool(int(os.environ.get('LENNY_DEBUG', 0)))
SEED = os.environ.get('LENNY_SEED')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
ADMIN_INTERNAL_SECRET = os.environ.get('ADMIN_INTERNAL_SECRET')
ADMIN_SALT = os.environ.get('ADMIN_SALT')
LOG_LEVEL = os.environ.get('LENNY_LOG_LEVEL', 'info')
SSL_CRT = os.environ.get('LENNY_SSL_CRT')
SSL_KEY = os.environ.get('LENNY_SSL_KEY')
LENNY_HTTP_HEADERS = {"User-Agent": "LennyImportBot/1.0"}
OTP_SERVER = os.environ.get('OTP_SERVER', 'https://openlibrary.org')
AUTH_MODE_DIRECT = False

# Open Library / Internet Archive credentials.
# Populated by `make ol-login`; empty means anonymous OL access.
OL_S3_ACCESS_KEY = os.environ.get('OL_S3_ACCESS_KEY') or None
OL_S3_SECRET_KEY = os.environ.get('OL_S3_SECRET_KEY') or None
OL_USERNAME = os.environ.get('OL_USERNAME') or None
LENDING_MODE = os.environ.get('LENNY_LENDING_MODE', 'none')  # none | ol | external
LENDING_ENABLED = LENDING_MODE != 'none'  # derived — True whenever any lending mode is active
OL_INDEXED = os.environ.get('LENNY_OL_INDEXED', 'false').lower() == 'true'

READER_PORT = int(os.environ.get('READER_PORT', 3000))
READIUM_PORT = int(os.environ.get('READIUM_PORT', 15080))
READIUM_BASE_URL = f"http://lenny_readium:{READIUM_PORT}"

LENNY_SEED = os.environ.get('LENNY_SEED')
# Boot/default values. These are cached per-worker at import; the authoritative
# cross-worker source is loan.env, read fresh by get_loan_limit() /
# get_loan_duration_days() below. The globals remain the fallback (and the
# monkeypatch seam used in tests, where loan.env does not exist).
LOAN_LIMIT         = int(os.environ.get('LENNY_LOAN_LIMIT', 10))
LOAN_DURATION_DAYS = int(os.environ.get('LENNY_LOAN_DURATION_DAYS', 0))  # 0 = never expire
# loan.env is the runtime-editable, cross-worker source of truth for loan policy
# (written by the admin endpoints). Read it per-request so all workers agree even
# though each caches its own boot-time globals above.
LOAN_ENV_PATH = '/app/loan.env'
AUTH_ENV_PATH = '/app/auth.env'
# ol.env is the cross-worker source of truth for the active lending mode
# (LENNY_LENDING_MODE), written by the admin endpoints. Same rationale as
# loan.env: read it fresh so every worker agrees after an admin change.
OL_ENV_PATH = '/app/ol.env'

OPTIONS = {
    'host': HOST,
    'port': PORT,
    'log_level': LOG_LEVEL,
    'reload': os.environ.get('LENNY_PRODUCTION', 'true').lower() == 'false',
    'workers': WORKERS,
}
if SSL_CRT and SSL_KEY:
    OPTIONS['ssl_keyfile'] = SSL_KEY
    OPTIONS['ssl_certfile'] = SSL_CRT
    SCHEME = 'https'

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

# Garage S3 configuration
S3_CONFIG = {
    'endpoint': os.environ.get('S3_ENDPOINT'),
    'access_key': os.environ.get('S3_ACCESS_KEY'),
    'secret_key': os.environ.get('S3_SECRET_KEY'),
    'secure': os.environ.get('S3_SECURE', 'false').lower() == 'true',
    'region': os.environ.get('S3_REGION', 'garage'),
}

# External OAuth / OIDC provider (optional — loaded from auth.env)
EXTERNAL_AUTH_ENABLED = os.environ.get('LENNY_EXTERNAL_AUTH_ENABLED', 'false').lower() == 'true'
IA_AUTH_ENABLED = os.environ.get('IA_AUTH_ENABLED', 'false').lower() == 'true'
OAUTH_CLIENT_ID       = os.environ.get('OAUTH_CLIENT_ID') or None
OAUTH_CLIENT_SECRET   = os.environ.get('OAUTH_CLIENT_SECRET') or None
OAUTH_DISCOVERY_URL   = os.environ.get('OAUTH_DISCOVERY_URL') or None
OAUTH_REDIRECT_URI    = os.environ.get('OAUTH_REDIRECT_URI') or None
OAUTH_SCOPES          = os.environ.get('OAUTH_SCOPES', 'openid email profile').split()
OAUTH_FLOW            = os.environ.get('OAUTH_FLOW', 'pkce')

def _read_env_value(path: str, key: str) -> Optional[str]:
    """Return the raw value of *key* from the env file at *path*, or ``None``.

    These env files are small (a handful of lines); reading them per request is
    cheap and guarantees every worker sees the same value after an admin edit,
    instead of serving the stale global cached at the worker's boot.
    """
    try:
        with open(path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}="):
                    return stripped.split("=", 1)[1]
    except OSError:
        return None
    return None


def _read_loan_env_value(key: str) -> Optional[str]:
    """Return the raw value of *key* from loan.env, or ``None`` if unavailable."""
    return _read_env_value(LOAN_ENV_PATH, key)


def read_lending_mode() -> str:
    """Authoritative active lending mode (ol.env → global fallback).

    Reads ``LENNY_LENDING_MODE`` fresh from ol.env so every worker agrees after
    an admin change. Falls back to the boot-time ``LENDING_MODE`` global when the
    file is absent (e.g. under TESTING, where ol.env does not exist and tests
    monkeypatch the global directly)."""
    raw = _read_env_value(OL_ENV_PATH, 'LENNY_LENDING_MODE')
    if raw is not None and raw != "":
        return raw
    return LENDING_MODE


def read_ia_auth_enabled() -> bool:
    """Authoritative IA_AUTH_ENABLED flag (auth.env → global fallback).

    Reads fresh from auth.env on every call so all workers agree immediately
    after an admin toggle — same pattern as read_lending_mode()."""
    raw = _read_env_value(AUTH_ENV_PATH, 'IA_AUTH_ENABLED')
    if raw is not None:
        return raw.strip().lower() == "true"
    return IA_AUTH_ENABLED


def get_loan_limit() -> int:
    """Authoritative max concurrent loans per patron (loan.env → global fallback)."""
    raw = _read_loan_env_value('LENNY_LOAN_LIMIT')
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return LOAN_LIMIT


def get_loan_duration_days() -> int:
    """Authoritative loan duration in days (loan.env → global fallback)."""
    raw = _read_loan_env_value('LENNY_LOAN_DURATION_DAYS')
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return LOAN_DURATION_DAYS


__all__ = ['SCHEME', 'HOST', 'PORT', 'DEBUG', 'OPTIONS', 'DB_URI', 'DB_CONFIG', 'S3_CONFIG', 'TESTING',
           'ADMIN_USERNAME', 'ADMIN_PASSWORD', 'ADMIN_INTERNAL_SECRET', 'ADMIN_SALT',
           'OL_S3_ACCESS_KEY', 'OL_S3_SECRET_KEY', 'OL_USERNAME', 'LENDING_MODE', 'LENDING_ENABLED', 'OL_INDEXED',
           'EXTERNAL_AUTH_ENABLED', 'IA_AUTH_ENABLED', 'OAUTH_CLIENT_ID', 'OAUTH_DISCOVERY_URL',
           'OAUTH_REDIRECT_URI', 'OAUTH_SCOPES', 'OAUTH_FLOW',
           'get_loan_limit', 'get_loan_duration_days',
           'read_lending_mode', 'OL_ENV_PATH']
