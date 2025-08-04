import pytest 

from lenny.core import auth

@pytest.fixture
def test_cookie():
    assert auth.create_session_cookie("example@archive.org") == "XXX"
