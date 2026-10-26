import pytest 

# Skip tests if dependencies not installed (for local development)
pytest.importorskip("itsdangerous")
from itsdangerous import URLSafeTimedSerializer
from lenny.core import auth

def test_cookie_basic_functionality():
    """Test cookie functionality without IP — always dict format now."""
    auth.SEED = b"123"
    auth.SERIALIZER = URLSafeTimedSerializer(auth.SEED, salt="auth-cookie")
    email = "example@archive.org"

    cookie = auth.create_session_cookie(email)

    # Cookie is always dict-format (contains {"email": ...})
    assert auth.get_authenticated_email(cookie) == email
    # verify_session_cookie also works (no IP binding when ip not provided)
    result = auth.verify_session_cookie(cookie)
    assert isinstance(result, dict)
    assert result["email"] == email
    
def test_cookie_with_ip_verification():
    """Test cookie functionality with IP verification"""
    # Setup test environment
    auth.SEED = b"123"
    auth.SERIALIZER = URLSafeTimedSerializer(auth.SEED, salt="auth-cookie")
    email = "example@archive.org"
    ip = "192.168.1.100"
    
    # Test new format (with IP)
    cookie = auth.create_session_cookie(email, ip)
    
    # Should be able to get email from cookie
    assert auth.get_authenticated_email(cookie) == email
    
    # Should verify successfully with correct IP
    result = auth.verify_session_cookie(cookie, ip)
    assert isinstance(result, dict)
    assert result['email'] == email
    
    # Should fail with wrong IP
    assert auth.verify_session_cookie(cookie, "192.168.1.101") is None
    
    # Should work without IP verification
    result = auth.verify_session_cookie(cookie)
    assert isinstance(result, dict)
    assert result['email'] == email

def test_otp_authenticate_with_ip():
    """Test OTP authentication with IP verification"""
    # Setup test environment
    auth.SEED = b"123"
    auth.SERIALIZER = URLSafeTimedSerializer(auth.SEED, salt="auth-cookie")
    email = "test@example.com"
    ip = "10.0.0.1"
    
    otp = "123456"
    
    # Mock the external OTP redeem call to return success
    import unittest.mock as mock
    with mock.patch.object(auth.OTP, 'redeem', return_value=True):
        # Authenticate with IP
        session_cookie = auth.OTP.authenticate(email, otp, ip)
        assert session_cookie is not None
        
        # Verify the cookie contains both email and IP
        result = auth.verify_session_cookie(session_cookie, ip)
        assert isinstance(result, dict)
        assert result['email'] == email
        
        # Should fail with wrong IP
        assert auth.verify_session_cookie(session_cookie, "10.0.0.2") is None
