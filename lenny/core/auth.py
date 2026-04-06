import hashlib
import logging
import httpx
from datetime import datetime, timedelta
from typing import Optional
from itsdangerous import URLSafeTimedSerializer, BadSignature
from lenny.configs import SEED, OTP_SERVER, ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_INTERNAL_SECRET, ADMIN_SALT
from lenny.core.cache import Cache
from lenny.core.exceptions import RateLimitError

logging.basicConfig(
    level=logging.DEBUG,  # Show DEBUG and higher
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("multipart").setLevel(logging.WARNING)

ADMIN_TOKEN_TTL = 86400  # 24 hours
ADMIN_SERIALIZER = None  # Initialized lazily

def _get_admin_serializer():
    global ADMIN_SERIALIZER
    if ADMIN_SERIALIZER is None:
        ADMIN_SERIALIZER = URLSafeTimedSerializer(SEED, salt=ADMIN_SALT)
    return ADMIN_SERIALIZER

def verify_admin_internal_secret(secret: str) -> bool:
    """Constant-time comparison to validate the internal shared secret."""
    if not ADMIN_INTERNAL_SECRET or not secret:
        return False
    return hmac.compare_digest(ADMIN_INTERNAL_SECRET, secret)

def authenticate_admin(username: str, password: str) -> Optional[str]:
    """Validates admin username + password and returns a signed token on success."""
    if not ADMIN_USERNAME or not ADMIN_PASSWORD:
        return None
    username_ok = hmac.compare_digest(ADMIN_USERNAME, username)
    password_ok = hmac.compare_digest(ADMIN_PASSWORD, password)
    if not (username_ok and password_ok):
        return None
    serializer = _get_admin_serializer()
    return serializer.dumps({"admin": True})

def verify_admin_token(token: str) -> bool:
    """Validates a signed admin token. Returns True if valid and not expired."""
    try:
        if not token:
            return False
        serializer = _get_admin_serializer()
        data = serializer.loads(token, max_age=ADMIN_TOKEN_TTL)
        return isinstance(data, dict) and data.get("admin") is True
    except BadSignature:
        return False

ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW_SECONDS = 60
SERIALIZER = None  # Will be initialized lazily
COOKIE_TTL = 604800

# Send-OTP limiter: 5 per 5 minutes
EMAIL_REQUEST_LIMIT = 5          
EMAIL_WINDOW_SECONDS = 300   
TIMEOUT = httpx.Timeout(connect=20.0, read=5.0, write=5.0, pool=5.0)

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

def verify_session_cookie(session, client_ip: str = None):
    """Retrieves and verifies data from signed cookie, optionally checking IP."""
    try:
        if not session:
            return None
        serializer = _get_serializer()
        data = serializer.loads(session, max_age=COOKIE_TTL)
        if isinstance(data, dict):
            # New format with IP verification
            stored_ip = data.get("ip")
            if client_ip and stored_ip and client_ip != stored_ip:
                return None  # IP mismatch
            return data
        else:
            # Old format, just email (no IP verification possible)
            return data
    except BadSignature:
        return None
        
class OTP:

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
        return Cache.is_throttled(
            "otp:send", email, EMAIL_REQUEST_LIMIT, EMAIL_WINDOW_SECONDS
        )

    @classmethod
    def issue(cls, email: str, ip_address: str) -> dict:
        """Interim: Use OpenLibrary.org to send & rate limit otp"""
        with httpx.Client(http2=True, verify=False, timeout=TIMEOUT) as client:
            return client.post(
                f"{OTP_SERVER}/account/otp/issue",
                params={"email": email, "ip": ip_address, "testing_access_key": "8593139480"},
                follow_redirects=False,
            ).json()

    @classmethod
    def redeem(cls, email: str, ip_address: str, otp: str) -> bool:
        with httpx.Client(http2=True, verify=False, timeout=TIMEOUT) as client:
            return "success" in client.post(
                f"{OTP_SERVER}/account/otp/redeem",
                params={"email": email, "ip": ip_address, "otp": otp, "testing_access_key": "8593139480"},
                follow_redirects=False
            ).json()

    @classmethod
    def is_rate_limited(cls, email: str) -> bool:
        """Returns True if the user is making too many OTP verification attempts."""
        return Cache.is_throttled(
            "otp:verify", email, ATTEMPT_LIMIT, ATTEMPT_WINDOW_SECONDS
        )

    @classmethod
    def authenticate(cls, email: str, otp: str, ip: str = None) -> Optional[str]:
        """
        Validates OTP for a window of past `OTP_VALID_MINUTES` and IP address.
        Returns a signed session cookie if authentication is successful.
        """
        if cls.verify(email, ip, otp):
            return create_session_cookie(email, ip)
        return None
