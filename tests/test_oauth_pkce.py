
import pytest
import hashlib
import base64
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs
from fastapi.testclient import TestClient
from lenny.app import app
from lenny.core import models
from lenny.core.db import session as db_session, init as db_init
from lenny.core.utils import encrypt_email
from unittest.mock import patch

client = TestClient(app)

# Helper to generate PKCE pair
def generate_pkce():
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
    return verifier, challenge

@pytest.fixture(autouse=True)
def setup_db():
    # Initialize DB (create tables)
    db_init()
    
    # Disable rate limiting for tests
    from lenny.core.limiter import limiter
    limiter.enabled = False
    try:
        db_session.query(models.RefreshToken).delete()
        db_session.query(models.AuthCode).delete()
        db_session.query(models.Client).delete()
        db_session.commit()
    except Exception:
        db_session.rollback()

    # 2. Seed client
    client_id = "test-client"
    redirect_uri = "http://localhost:3000/callback"
    
    try:
        client_obj = models.Client(
            client_id=client_id,
            redirect_uris="http://localhost:3000/callback,opds://callback", 
            is_confidential=False
        )
        db_session.add(client_obj)
        db_session.commit()
    except Exception:
        db_session.rollback()
        
    yield
    
    # 3. Teardown
    try:
        db_session.query(models.RefreshToken).delete()
        db_session.query(models.AuthCode).delete()
        db_session.query(models.Client).delete()
        db_session.commit()
    except Exception:
        db_session.rollback()
    
    db_session.close()

def test_authorize_unauthenticated_shows_login_form():
    """
    Test that an unauthenticated GET to /authorize renders the OTP login form.
    """
    verifier, challenge = generate_pkce()
    state = "xyz123"
    client_id = "test-client"
    redirect_uri = "http://localhost:3000/callback"
    
    response = client.get(
        "/v1/oauth/authorize",
        params={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        },
        follow_redirects=False 
    )
    assert response.status_code == 200

def test_pkce_enforcement():
    """
    Test that requests without code_challenge or with wrong method are rejected.
    """
    response = client.get(
        "/v1/oauth/authorize",
        params={
            "client_id": "test-client",
            "redirect_uri": "http://localhost:3000/callback",
            "state": "xyz",
        }
    )
    assert response.status_code == 400

    response = client.get(
        "/v1/oauth/authorize",
        params={
            "client_id": "test-client",
            "redirect_uri": "http://localhost:3000/callback",
            "state": "xyz",
            "code_challenge": "foo",
            "code_challenge_method": "plain" 
        }
    )
    assert response.status_code == 400
    assert "S256" in response.text

def test_client_validation():
    """
    Test invalid client_id or redirect_uri.
    """
    verifier, challenge = generate_pkce()
    
    # Wrong Client ID
    response = client.get(
        "/v1/oauth/authorize",
        params={
            "client_id": "bad-client",
            "redirect_uri": "http://localhost:3000/callback",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }
    )
    assert response.status_code == 400
    
    # Wrong Redirect URI
    response = client.get(
        "/v1/oauth/authorize",
        params={
            "client_id": "test-client",
            "redirect_uri": "http://evil.com/callback",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256"
        }
    )
    assert response.status_code == 400

def test_full_messages_flow():
    """
    Simulate full flow:
    1. Authorize (POST with OTP) -> Get Code
    2. Token (POST with Code + Verifier) -> Get Token
    """
    verifier, challenge = generate_pkce()
    state = "xyz-full-flow"
    client_id = "test-client"
    redirect_uri = "http://localhost:3000/callback"
    email = "test@example.com"
    otp = "000000" 
    
    with patch("lenny.core.auth.OTP.authenticate") as mock_auth:
        mock_auth.return_value = "valid_session_cookie"
        
        # 1. Authorize (POST credentials)
        response = client.post(
            "/v1/oauth/authorize",
            data={
                "email": email,
                "otp": otp,
            },
            params={ 
                 "client_id": client_id,
                 "redirect_uri": redirect_uri,
                 "state": state,
                 "code_challenge": challenge,
                 "code_challenge_method": "S256"
            },
            follow_redirects=False
        )
        
        assert response.status_code == 302
        location = response.headers["location"]
        assert redirect_uri in location
        assert "code=" in location
        
        # Extract code
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        code = params["code"][0]
        
        # 2. Exchange Token
        token_response = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier
            }
        )
        
        assert token_response.status_code == 200
        token_data = token_response.json()
        assert "access_token" in token_data
        assert "refresh_token" in token_data

        # 3. Verify Token Replay (Should fail)
        replay_response = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier
            }
        )
        assert replay_response.status_code == 400
        assert "Authorization code already used" in replay_response.json()["error_description"]

        # 4. Refresh Token Flow
        refresh_response = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": token_data["refresh_token"]
            }
        )
        assert refresh_response.status_code == 200
        refresh_data = refresh_response.json()
        assert "access_token" in refresh_data
        assert "refresh_token" in refresh_data
        # New refresh token should differ (rotation)
        assert refresh_data["refresh_token"] != token_data["refresh_token"]

        # 5. Old refresh token should be revoked (replay)
        replay_refresh = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": token_data["refresh_token"]
            }
        )
        assert replay_refresh.status_code == 400
        assert "already used" in replay_refresh.json()["error_description"]

def test_bad_verifier():
    """
    Test token exchange with wrong verifier.
    """
    verifier, challenge = generate_pkce()
    wrong_verifier = "a" * 43
    
    code = "manual_test_code"
    client_id = "test-client"
    email = "manual@test.com"
    
    encrypted_email = encrypt_email(email)
    
    auth_code = models.AuthCode(
        code=code,
        client_id=client_id,
        redirect_uri="http://localhost:3000/callback",
        email_encrypted=encrypted_email, # New field name
        state="state",
        scope="openid",
        code_challenge=challenge,
        code_challenge_method="S256",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5) # Timezone aware
    )
    db_session.add(auth_code)
    db_session.commit()
    
    response = client.post(
        "/v1/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost:3000/callback",
            "client_id": client_id,
            "code_verifier": wrong_verifier 
        }
    )
    assert response.status_code == 400
    assert "PKCE verification failed" in response.json()["error_description"]

def test_authorize_opds_redirect():
    """
    Test that opds:// redirect URIs return a success page (200) instead of a 302 redirect.
    """
    verifier, challenge = generate_pkce()
    
    # Mock authentication to test the opds:// redirect path
    # get_authenticated_email must be patched so it returns a verified email
    
    with patch("lenny.routes.oauth.get_authenticated_email", return_value="test@example.com"):
        response = client.get(
            "/v1/oauth/authorize",
            params={
                "client_id": "test-client",
                "redirect_uri": "opds://callback",
                "state": "xyz123",
                "code_challenge": challenge,
                "code_challenge_method": "S256"
            },
            cookies={"session": "valid_session"}
        )
        
        assert response.status_code == 200
        assert "Rendered: oauth_success.html" in response.text

def test_opds_auth_document():
    """
    Verify that the OPDS Authentication Document is returned correctly.
    """
    response = client.get("/v1/oauth/implicit")
    assert response.status_code == 200
    assert response.headers.get('content-type') == 'application/opds-authentication+json'

    data = response.json()

    # OPDS 2.0 Authentication Document structure puts these inside 'authentication' array
    assert "authentication" in data
    assert len(data["authentication"]) > 0
    auth_method = data["authentication"][0]
    
    assert "http://opds-spec.org/auth/oauth" in auth_method["type"]

    links = {l["rel"]: l for l in auth_method["links"]}
    assert "authenticate" in links
    assert "oauth/authorize" in links["authenticate"]["href"]
    assert links["authenticate"]["type"] == "text/html"

def test_refresh_token_wrong_client():
    """
    Test that refresh tokens can't be used with a different client_id.
    """
    verifier, challenge = generate_pkce()
    email = "test@example.com"
    
    with patch("lenny.core.auth.OTP.authenticate") as mock_auth:
        mock_auth.return_value = "valid_session_cookie"
        
        response = client.post(
            "/v1/oauth/authorize",
            data={"email": email, "otp": "000000"},
            params={
                "client_id": "test-client",
                "redirect_uri": "http://localhost:3000/callback",
                "state": "xyz",
                "code_challenge": challenge,
                "code_challenge_method": "S256"
            },
            follow_redirects=False
        )
        
        parsed = urlparse(response.headers["location"])
        params = parse_qs(parsed.query)
        code = params["code"][0]
        
        token_response = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "http://localhost:3000/callback",
                "client_id": "test-client",
                "code_verifier": verifier
            }
        )
        refresh_tok = token_response.json()["refresh_token"]
        
        # Try with wrong client_id
        bad_response = client.post(
            "/v1/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": "wrong-client",
                "refresh_token": refresh_tok
            }
        )
        assert bad_response.status_code == 400
