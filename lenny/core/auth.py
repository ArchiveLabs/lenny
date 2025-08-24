import hashlib
import hmac
import time
from datetime import datetime, timedelta
from typing import Optional
from itsdangerous import URLSafeTimedSerializer, BadSignature
from lenny.configs import SEED
from lenny.core.exceptions import RateLimitError

OTP_VALID_MINUTES = 10
ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW_SECONDS = 60
SERIALIZER = URLSafeTimedSerializer(SEED, salt="auth-cookie")
# Session cookie lifetime (seconds): 1 week
COOKIE_TTL = 604800

def create_session_cookie(email: str, ip: str = None) -> str:
    """Returns a signed + encrypted session cookie."""
    if ip:
        # New format: serialize both email and IP
        data = {"email": email, "ip": ip}
        return SERIALIZER.dumps(data)
    else:
        # Backward compatibility: serialize just email
        return SERIALIZER.dumps(email)

def get_authenticated_email(session) -> Optional[str]:
    """Retrieves and verifies email from signed cookie."""
    try:
        data = SERIALIZER.loads(session, max_age=COOKIE_TTL)
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
        data = SERIALIZER.loads(session, max_age=COOKIE_TTL)
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

    @staticmethod
    def generate(email: str, ip_address: str, issued_minute: Optional[int] = None) -> str:
        """Generates an OTP for a given email, IP address, and timestamp."""
        now = int(time.time() // 60)
        ts = issued_minute or now
        payload = f"{email}:{ip_address}:{ts}".encode()
        return hmac.new(SEED, payload, hashlib.sha256).hexdigest()

    @classmethod
    def verify(cls, email: str, ip_address: str, ts: str, otp: str) -> bool:
        if cls.is_rate_limited(email):
            raise RateLimitError
        expected_otp = cls.generate(email, ip_address, ts)
        return hmac.compare_digest(otp, expected_otp)

    @classmethod
    def sendmail(cls, email: str, ip_address: str, url: str):
        """Interim: Use OpenLibrary.org to send & rate limit otp"""
        # TODO: send otp via Open Library
        otp = cls.generate(email, ip_address)
        params = {
            "email": email,
            "ip_address": ip_address,
            "url": url,
            "otp": otp,
        }
        headers = {"authorization": "..."}
        # e.g. r = requests.post("https://openlibrary.org/api/auth", params=params, headers=headers)

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
        now_minute = int(time.time() // 60)
        for delta in range(OTP_VALID_MINUTES):
            ts = now_minute - delta
            if cls.verify(email, ts, otp):
                return create_session_cookie(email, ip)
        return None
