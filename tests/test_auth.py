import pytest 

from lenny.core import auth

@pytest.fixture
def test_cookie(monkeypatch):
    monkeypatch.setattr(auth, "SEED", "123")
    email = "example@archive.org"
    cookie = auth.create_session_cookie(email)
    assert cookie.startswith("ImV4YW1wbGVAYXJjaGl2ZS5vcmci")
    assert auth.get_authenticated_email(cookie) == email
