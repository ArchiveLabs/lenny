import pytest 

from itsdangerous import URLSafeTimedSerializer
from lenny.core import auth

@pytest.fixture
def test_cookie(monkeypatch):
    monkeypatch.setattr(auth, "SEED", "123")
    monkeypatch.setattr(auth, "SERIALIZER", URLSafeTimedSerializer(auth.SEED, salt="auth-cookie"))
    email = "example@archive.org"
    expected_prefix = "ImV4YW1wbGVAYXJjaGl2ZS5vcmci"
    
    cookie = auth.create_session_cookie(email)
    
    assert cookie.startswith(expected_prefix), f"cookie prefix mismatch: {cookie!r}"
    assert auth.get_authenticated_email(cookie) == email
