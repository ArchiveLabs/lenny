import hashlib
import hmac
import logging
import time
import requests
from datetime import datetime, timedelta
from typing import Optional
from itsdangerous import URLSafeTimedSerializer, BadSignature
from lenny.configs import SEED, OTP_SERVER
from lenny.core.exceptions import RateLimitError

logger = logging.getLogger(__name__)

ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW_SECONDS = 60
SERIALIZER = None  # Will be initialized lazily
COOKIE_TTL = 604800

# Send-OTP limiter: 5 per 5 minutes
EMAIL_REQUEST_LIMIT = 5          
EMAIL_WINDOW_SECONDS = 300   

def _get_serializer():
    """Get or initialize the SERIALIZER lazily."""
    global SERIALIZER
    if SERIALIZER is None:
        SERIALIZER = URLSafeTimedSerializer(SEED, salt="auth-cookie")
    return SERIALIZER

def create_session_cookie(email: str, ip: str = None) -> str:
    """Returns a signed + encrypted session cookie."""
    serializer = _get_serializer()
    if ip:
        # New format: serialize both email and IP (no need to store SEED in cookie)
        data = {"email": email, "ip": ip}
        return serializer.dumps(data)
    else:
        # Backward compatibility: serialize just email
        return serializer.dumps(email)

def get_authenticated_email(session) -> Optional[str]:
    """Retrieves and verifies email from signed cookie."""
    try:
        serializer = _get_serializer()
        data = serializer.loads(session, max_age=COOKIE_TTL)
        if isinstance(data, dict):
            # New format with IP
            return data.get("email")
        else:
            # Old format, just email
            return data
    except BadSignature:
        return None

def verify_session_cookie(session, client_ip: str = None) -> Optional[str]:
    """Retrieves and verifies email from signed cookie, optionally checking IP."""
    try:
        if not session:
            return None
        serializer = _get_serializer()
        data = serializer.loads(session, max_age=COOKIE_TTL)
        if isinstance(data, dict):
            # New format with IP verification
            email = data.get("email")
            stored_ip = data.get("ip")
            if client_ip and stored_ip and client_ip != stored_ip:
                return None  # IP mismatch
            return email
        else:
            # Old format, just email (no IP verification possible)
            return data
    except BadSignature:
        return None
        
class OTP:

    _attempts = {}
    _send_attempts = {}

    @classmethod
    def generate(cls, email: str, issued_minute: int = None) -> str:
        """
        Generate a simple OTP for testing purposes.
        This is a stub method - production OTP generation happens on the OTP server.
        """
        if issued_minute is None:
            issued_minute = datetime.now().minute
        
        # Create a simple deterministic OTP for testing
        otp_string = f"{email}{SEED}{issued_minute}"
        return hashlib.sha256(otp_string.encode()).hexdigest()[:6]

    @classmethod
    def verify(cls, email: str, ip_address: str, otp: str) -> bool:
        """Verifies OTP for email and IP address, with rate limiting."""
        if cls.is_rate_limited(email):
            raise RateLimitError("Too many attempts. Please try again later.")
        otp_redemption = cls.redeem(email, ip_address, otp)
        if otp_redemption:
            return True
        return False 
    
    @classmethod
    def is_send_rate_limited(cls, email: str) -> bool:
        """Limit OTP send requests: 5 emails per 5 minutes per email."""
        now = time.time()
        attempts = cls._send_attempts.get(email, [])
        attempts = [ts for ts in attempts if now - ts < EMAIL_WINDOW_SECONDS]
        cls._send_attempts[email] = attempts + [now]
        return len(attempts) >= EMAIL_REQUEST_LIMIT

    @classmethod
    def issue(cls, email: str, ip_address: str) -> dict:
        """Interim: Use OpenLibrary.org to send & rate limit otp"""
        return requests.post(f"{OTP_SERVER}/account/otp/issue", params={
            "email": email,
            "ip": ip_address,
        }).json()

    @classmethod
    def redeem(cls, email: str, ip_address: str, otp: str) -> bool:
        data = requests.post(f"{OTP_SERVER}/account/otp/redeem", params={
            "email": email,
            "ip": ip_address,
            "otp": otp,
        }).json()
        if "success" not in data:
            return False
        return True

    @classmethod
    def is_rate_limited(cls, email: str) -> bool:
        """Updates attempts within timeframe for email and
        returns True if the user is making too many attempts.
        """
        now = time.time()
        attempts = cls._attempts.get(email, [])
        # Keep only recent attempts
        attempts = [ts for ts in attempts if now - ts < ATTEMPT_WINDOW_SECONDS]
        cls._attempts[email] = attempts + [now]
        return len(attempts) >= ATTEMPT_LIMIT

    @classmethod
    def authenticate(cls, email: str, otp: str, ip: str = None) -> Optional[str]:
        """
        Validates OTP for a window of past `OTP_VALID_MINUTES` and IP address.
        Returns a signed session cookie if authentication is successful.
        """
        if cls.verify(email, ip, otp):
            return create_session_cookie(email, ip)
        return None
