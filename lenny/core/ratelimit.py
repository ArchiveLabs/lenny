
"""
Rate limiting configuration for Lenny.

This module provides rate limiting settings that are compatible between
Nginx limit_req and slowapi, ensuring consistent TTL values.

:copyright: (c) 2015 by AUTHORS
:license: see LICENSE for more details
"""

import os
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Rate limit window in seconds - must be compatible between nginx and slowapi
# Using 60 seconds (1 minute) as a standard window
RATE_LIMIT_WINDOW = int(os.environ.get('RATE_LIMIT_WINDOW', 60))

def _parse_rate_limit(env_var: str, default: int) -> int:
    """
    Parse rate limit from environment variable.
    Supports both integer format (100) and string format ('100/minute').
    Returns the integer count of requests.
    """
    value = os.environ.get(env_var, str(default))
    if '/' in str(value):
        return int(value.split('/')[0])
    return int(value)

# Supports both integer (100) and string ('100/minute') formats for backward compatibility
RATE_LIMIT_GENERAL_COUNT = _parse_rate_limit('RATE_LIMIT_GENERAL', 100)
RATE_LIMIT_LENIENT_COUNT = _parse_rate_limit('RATE_LIMIT_LENIENT', 300)
RATE_LIMIT_STRICT_COUNT = _parse_rate_limit('RATE_LIMIT_STRICT', 20)

RATE_LIMIT_GENERAL = f'{RATE_LIMIT_GENERAL_COUNT}/minute'
RATE_LIMIT_LENIENT = f'{RATE_LIMIT_LENIENT_COUNT}/minute'
RATE_LIMIT_STRICT = f'{RATE_LIMIT_STRICT_COUNT}/minute'

# Create the limiter instance
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_GENERAL],
    headers_enabled=True,
)

def init_rate_limiter(app):
    """
    Initialize rate limiting for the FastAPI app.
    
    Args:
        app: FastAPI application instance
    """
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

