import hashlib
import hmac
import logging
import httpx
from datetime import datetime, timedelta
from typing import Optional
from itsdangerous import URLSafeTimedSerializer, BadSignature
from lenny.configs import SEED, OTP_SERVER, ADMIN_USERNAME, ADMIN_PASSWORD, ADMIN_INTERNAL_SECRET, ADMIN_SALT, LOG_LEVEL
from lenny.core.openlibrary import ol_auth_headers, _REDACT_HOOKS
from lenny.core.exceptions import LendingNotConfiguredError, OTPGenerationError
from lenny.core.cache import Cache
from lenny.core.exceptions import RateLimitError

logging.basicConfig(
    level=LOG_LEVEL.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)
logging.getLogger("multipart").setLevel(logging.WARNING)
# boto3/botocore DEBUG logs full request/response detail (object keys, sizes,
# access key ID, signing internals) — noisy and exposes storage layout even
# when it doesn't leak the actual secret key. Keep quiet regardless of LOG_LEVEL.
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("s3transfer").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

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

# Open Library's OTP endpoints answer HTTP 200 with a JSON `{"error": ...}` body
# on every failure, so the error string is the only signal there is. Map the ones
# it can return (openlibrary/plugins/upstream/account.py: otp_service_issue,
# otp_service_redeem, _require_s3_auth) onto something a patron can act on, while
# the operator gets the raw code in the log.
OL_ERROR_FALLBACK = "Open Library could not send a one-time password right now."
OL_ERROR_MESSAGES = {
    # Our own credentials are the problem, not anything the patron did.
    "missing_or_invalid_authorization": (
        "This library's Open Library credentials are missing or malformed. "
        "An administrator needs to run `make ol-login`."
    ),
    "unauthorized": (
        "This library's Open Library credentials were rejected. "
        "An administrator needs to run `make ol-login`."
    ),
    "auth_service_unavailable": (
        "Open Library's login service is temporarily unavailable. Please try again shortly."
    ),
    "ratelimit": "Too many requests. Please wait a minute and try again.",
    "missing_keys": "Lenny sent an incomplete request to Open Library.",
    "challenge_failed": "Open Library could not verify this library's identity.",
    "otp_mismatch": "That one-time password is not valid.",
}

ATTEMPT_LIMIT = 5
ATTEMPT_WINDOW_SECONDS = 60
SERIALIZER = None  # Will be initialized lazily
COOKIE_TTL = 604800

# Send-OTP limiter: 5 per 5 minutes
EMAIL_REQUEST_LIMIT = 5          
EMAIL_WINDOW_SECONDS = 300   
# `read` covers Open Library's whole handler, which now validates our IA S3 keys
# against xauthn on every OTP request — a second network hop we do not control.
# At the previous 5s this timed out under ordinary xauthn latency and the failure
# was swallowed, so the patron saw a generic "please try again" forever.
TIMEOUT = httpx.Timeout(connect=20.0, read=20.0, write=5.0, pool=5.0)

def _get_serializer():
    """Get or initialize the SERIALIZER lazily."""
    global SERIALIZER
    if SERIALIZER is None:
        SERIALIZER = URLSafeTimedSerializer(SEED, salt="auth-cookie")
    return SERIALIZER

def create_session_cookie(email: str, ip: str = None) -> str:
    """Returns a signed + encrypted session cookie. Always uses dict format."""
    serializer = _get_serializer()
    data = {"email": email}
    if ip:
        data["ip"] = ip
    return serializer.dumps(data)

def get_authenticated_email(session) -> Optional[str]:
    """Retrieves and verifies email from signed cookie. Rejects old-format (non-dict) cookies."""
    try:
        serializer = _get_serializer()
        data = serializer.loads(session, max_age=COOKIE_TTL)
        if isinstance(data, dict):
            return data.get("email")
        return None  # old-format plain-email cookies rejected
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
            stored_ip = data.get("ip")
            if client_ip and stored_ip and client_ip != stored_ip:
                return None  # IP mismatch
            return data
        # Old-format cookies (plain email string) lack IP binding — reject them
        # so stolen cookies from before IP binding cannot be replayed.
        return None
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
    def _check_lending_enabled(cls) -> None:
        from lenny import configs
        # File-fresh read so the OTP borrow gate agrees across workers right
        # after an admin mode change (per-worker globals can be stale).
        if configs.read_lending_mode() != 'ol':
            raise LendingNotConfiguredError(
                "OL lending is not the active lending mode. "
                "Set LENNY_LENDING_MODE=ol via the admin panel."
            )
        if not (configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY):
            raise LendingNotConfiguredError(
                "OL lending is active but credentials are missing. Run 'make ol-login'."
            )

    @staticmethod
    def _mask_email(email: str) -> str:
        """`alice@example.org` -> `al***@example.org`, so a log line is useful
        for support without writing a patron's full address to disk."""
        local, _, domain = (email or "").partition("@")
        return f"{local[:2]}***@{domain}" if domain else "***"

    @classmethod
    def _read_response(cls, response: httpx.Response, operation: str, email: str) -> dict:
        """Turn an Open Library OTP response into a dict, or raise.

        Open Library answers **HTTP 200 with a JSON error body** on every failure
        path — `delegate.RawText` never raises, so there is no non-2xx and nothing
        for Sentry to catch. Callers used to discard this response entirely, which
        made a failed OTP invisible on both sides: Lenny would tell the patron
        "we sent you a password" while Open Library had refused the request.
        Parse the body, log the reason, and raise so a caller cannot ignore it.
        """
        masked = cls._mask_email(email)
        try:
            payload = response.json()
        except ValueError:
            # Not JSON at all — a WAF block page, an HTML error, a redirect body.
            logger.error(
                "OTP %s: non-JSON response from %s (HTTP %s, %d bytes) for %s",
                operation, OTP_SERVER, response.status_code, len(response.content), masked,
            )
            raise OTPGenerationError("Open Library returned an unexpected response.")

        if not isinstance(payload, dict):
            logger.error("OTP %s: unexpected payload type %s for %s",
                         operation, type(payload).__name__, masked)
            raise OTPGenerationError("Open Library returned an unexpected response.")

        if error := payload.get("error"):
            # The whole point of this method: say out loud which one it was.
            logger.error(
                "OTP %s refused by %s for %s: error=%s detail=%s (HTTP %s)",
                operation, OTP_SERVER, masked, error,
                {k: v for k, v in payload.items() if k != "error"} or "-",
                response.status_code,
            )
            raise OTPGenerationError(OL_ERROR_MESSAGES.get(error, OL_ERROR_FALLBACK), code=error)

        return payload

    @classmethod
    def _post(cls, path: str, params: dict, operation: str, email: str) -> dict:
        cls._check_lending_enabled()
        try:
            with httpx.Client(http2=True, verify=False, timeout=TIMEOUT,
                              event_hooks=_REDACT_HOOKS) as client:
                response = client.post(
                    f"{OTP_SERVER}{path}",
                    params=params,
                    headers=ol_auth_headers(),
                    follow_redirects=False,
                )
        except httpx.TimeoutException as e:
            # Open Library validates our S3 keys against xauthn on every OTP
            # request, so this call is not purely local to OL and can be slow.
            logger.error("OTP %s timed out talking to %s for %s: %s",
                         operation, OTP_SERVER, cls._mask_email(email), e)
            raise OTPGenerationError("Open Library did not respond in time.", code="timeout")
        except httpx.HTTPError as e:
            logger.error("OTP %s transport error talking to %s for %s: %s",
                         operation, OTP_SERVER, cls._mask_email(email), e)
            raise OTPGenerationError("Could not reach Open Library.", code="transport")

        return cls._read_response(response, operation, email)

    @classmethod
    def issue(cls, email: str, ip_address: str) -> dict:
        """Ask Open Library to email `email` a one-time password.

        Returns Open Library's success payload. Raises `OTPGenerationError` (with
        a `.code` carrying Open Library's own error string) if it refused —
        never returns quietly on failure.
        """
        return cls._post(
            "/account/otp/issue",
            {"email": email, "ip": ip_address},
            "issue",
            email,
        )

    @classmethod
    def redeem(cls, email: str, ip_address: str, otp: str) -> bool:
        """True if Open Library accepted this OTP, False if it was simply wrong.

        Anything that is *not* a wrong password — bad credentials, rate limiting,
        a timeout — raises instead of returning False. Collapsing those into
        False told the patron "Invalid OTP" when the real problem was that
        Lenny's own Open Library credentials had gone stale, which is a bug
        report nobody can act on.
        """
        try:
            payload = cls._post(
                "/account/otp/redeem",
                {"email": email, "ip": ip_address, "otp": otp},
                "redeem",
                email,
            )
        except OTPGenerationError as e:
            if e.code == "otp_mismatch":
                return False
            raise
        return "success" in payload

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
