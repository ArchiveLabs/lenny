import pytest 

from itsdangerous import URLSafeTimedSerializer
from lenny.core import auth

def test_cookie_basic_functionality():
    """Test basic cookie functionality without IP verification (backward compatibility)"""
    # Setup test environment
    auth.SEED = b"123"
    auth.SERIALIZER = URLSafeTimedSerializer(auth.SEED, salt="auth-cookie")
    email = "example@archive.org"
    expected_prefix = "ImV4YW1wbGVAYXJjaGl2ZS5vcmci"
    
    # Test old format (without IP)
    cookie = auth.create_session_cookie(email)
    
    assert cookie.startswith(expected_prefix), f"cookie prefix mismatch: {cookie!r}"
    assert auth.get_authenticated_email(cookie) == email
    
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
    assert auth.verify_session_cookie(cookie, ip) == email
    
    # Should fail with wrong IP
    assert auth.verify_session_cookie(cookie, "192.168.1.101") is None
    
    # Should work without IP verification
    assert auth.verify_session_cookie(cookie) == email

def test_otp_authenticate_with_ip():
    """Test OTP authentication with IP verification"""
    # Setup test environment
    auth.SEED = b"123"
    auth.SERIALIZER = URLSafeTimedSerializer(auth.SEED, salt="auth-cookie")
    email = "test@example.com"
    ip = "10.0.0.1"
    
    # Generate an OTP (pass None for issued_minute to use current time)
    otp = auth.OTP.generate(email, None)
    
    # Mock the external OTP redeem call to return success
    import unittest.mock as mock
    with mock.patch.object(auth.OTP, 'redeem', return_value=True):
        # Authenticate with IP
        session_cookie = auth.OTP.authenticate(email, otp, ip)
        assert session_cookie is not None
        
        # Verify the cookie contains both email and IP
        verified_email = auth.verify_session_cookie(session_cookie, ip)
        assert verified_email == email
        
        # Should fail with wrong IP
        assert auth.verify_session_cookie(session_cookie, "10.0.0.2") is None
