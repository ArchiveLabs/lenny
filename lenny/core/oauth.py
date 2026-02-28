
import hashlib
import base64
import secrets
import jwt
from datetime import datetime, timedelta, timezone
from lenny.core.models import AuthCode, Client, RefreshToken
from lenny.core.utils import encrypt_email, decrypt_email
from lenny.configs import LENNY_SEED

class OAuthService:
    @staticmethod
    def validate_client(client_id: str, redirect_uri: str) -> bool:
        """
        Validates that the client exists and the redirect_uri is allowed.
        Accepts exact string matches.
        """
        client = Client.get_by_id(client_id)
        if not client:
            return False
        
        return client.is_valid_redirect_uri(redirect_uri)

    @staticmethod
    def create_authorization_code(
        client_id: str,
        redirect_uri: str,
        email: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str = 'S256',
        scope: str = "openid"
    ) -> str:
        """
        Generates and stores an authorization code.
        """
        if not code_challenge:
            raise ValueError("code_challenge is required")

        if code_challenge_method != 'S256':
            raise ValueError("Only S256 code_challenge_method is supported")

        # Generate high-entropy code
        code = secrets.token_urlsafe(64)
        
        # Encrypt email
        encrypted_email = encrypt_email(email)
        
        AuthCode.create(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            email_encrypted=encrypted_email,
            state=state,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            used=False
        )
        
        return code

    @staticmethod
    def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
        """
        Verifies the code_verifier against the code_challenge using the specified method.
        Only S256 is supported.
        """
        if method != 'S256':
            return False
        
        if not code_challenge:
            return False
            
        if not code_verifier:
            return False

        try:
            # Calculate S256(verifier)
            digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
            calculated_challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
        except UnicodeEncodeError:
            return False
        
        return secrets.compare_digest(calculated_challenge, code_challenge)

    @staticmethod
    def exchange_code(
        client_id: str,
        code: str,
        redirect_uri: str,
        code_verifier: str
    ) -> dict:
        """
        Exchanges authorization code for an access token.
        Returns dict with access_token, token_type, expires_in, scope.
        Raises ValueError on failure.
        """
        # Atomic update to prevent race conditions (TOCTOU)
        success = AuthCode.mark_as_used(code)
        
        if not success:
            # Check if it was a replay or invalid code
            auth_code = AuthCode.get_by_code(code)
            if auth_code and auth_code.used:
                raise ValueError("Authorization code already used")
            raise ValueError("Invalid authorization code")
        
        # Fetch the code record (we know it exists and we just marked it used)
        auth_code = AuthCode.get_by_code(code)
        if not auth_code:
            raise ValueError("Invalid authorization code")
            
        expires_at = auth_code.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
            
        if expires_at < datetime.now(timezone.utc):
            raise ValueError("Authorization code expired")
            
        if auth_code.client_id != client_id:
            raise ValueError("Client ID mismatch")
            
        if auth_code.redirect_uri != redirect_uri:
            raise ValueError("Redirect URI mismatch")
            
        # Verify PKCE
        if not OAuthService.verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
            raise ValueError("PKCE verification failed")

        # Decrypt email
        email = decrypt_email(auth_code.email_encrypted)
        
        # Generate tokens
        ttl_minutes = 60
        access_token = OAuthService.generate_jwt(client_id, email, auth_code.scope, ttl_minutes=ttl_minutes)
        refresh_token = OAuthService._create_refresh_token(
            client_id=client_id,
            email=email,
            scope=auth_code.scope,
        )
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": ttl_minutes * 60,
            "scope": auth_code.scope
        }

    @staticmethod
    def generate_jwt(client_id: str, email: str, scope: str, ttl_minutes: int = 60) -> str:
        """
        Generates a signed JWT access token.
        """
        now = datetime.now(timezone.utc)
        payload = {
            "iss": "lenny-auth-server",
            "sub": email,
            "aud": "lenny-api",
            "azp": client_id,
            "exp": now + timedelta(minutes=ttl_minutes),
            "iat": now,
            "scope": scope
        }
        
        # HS256 (HMAC-SHA256) signing with LENNY_SEED
        # Note: S256 in PKCE refers to SHA-256 code challenge hashing, not JWT signing
        # In production, prefer asymmetric algorithms (RS256)
        token = jwt.encode(payload, LENNY_SEED, algorithm="HS256")
        return token

    @staticmethod
    def _create_refresh_token(client_id: str, email: str, scope: str, ttl_days: int = 30) -> str:
        """Creates and stores a refresh token. Returns the token string."""
        token = secrets.token_urlsafe(64)
        encrypted_email = encrypt_email(email)
        RefreshToken.create(
            token=token,
            client_id=client_id,
            email_encrypted=encrypted_email,
            scope=scope,
            expires_at=datetime.now(timezone.utc) + timedelta(days=ttl_days),
            revoked=False,
        )
        return token

    @staticmethod
    def refresh_access_token(client_id: str, refresh_token: str) -> dict:
        """
        Exchanges a refresh token for a new access token + new refresh token.
        Implements token rotation: old refresh token is revoked.
        Raises ValueError on failure.
        """
        # Atomically revoke to prevent reuse
        revoked = RefreshToken.revoke(refresh_token)
        if not revoked:
            existing = RefreshToken.get_by_token(refresh_token)
            if existing and existing.revoked:
                raise ValueError("Refresh token already used")
            raise ValueError("Invalid refresh token")

        # Fetch the record (just revoked, so it exists)
        token_record = RefreshToken.get_by_token(refresh_token)
        if not token_record:
            raise ValueError("Invalid refresh token")

        # Check expiry
        expires_at = token_record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < datetime.now(timezone.utc):
            raise ValueError("Refresh token expired")

        # Check client binding
        if token_record.client_id != client_id:
            raise ValueError("Client ID mismatch")

        # Decrypt email
        email = decrypt_email(token_record.email_encrypted)

        # Issue new token pair
        ttl_minutes = 60
        access_token = OAuthService.generate_jwt(client_id, email, token_record.scope, ttl_minutes=ttl_minutes)
        new_refresh_token = OAuthService._create_refresh_token(
            client_id=client_id,
            email=email,
            scope=token_record.scope,
        )
        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": ttl_minutes * 60,
            "scope": token_record.scope
        }
