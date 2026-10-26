"""Share links for the library: the OPDS feed, a per-book OPDS address and the built-in reader link.
They come from the configured public address (LENNY_PROXY, else host and port), never from the request."""

import os
from unittest.mock import patch

import pytest

os.environ.setdefault("TESTING", "true")
SECRET = "internal-secret-for-tests"
URL = "/v1/api/admin/library/links"


@pytest.fixture(scope="module")
def app_module():
    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app
        yield app


@pytest.fixture
def headers():
    from lenny.core import auth
    with patch.object(auth, "ADMIN_INTERNAL_SECRET", SECRET), patch.object(auth, "ADMIN_SALT", "test-admin-salt"), \
         patch.object(auth, "ADMIN_SERIALIZER", None):
        yield {"X-Admin-Internal-Secret": SECRET, "Authorization": f"Bearer {auth.issue_admin_token()}"}


def links(app_module, headers, proxy="", scheme="http", host="localhost", port=8080, extra=None, base="http://testserver"):
    """Ask for the links with the node configured as given (the way .env would)."""
    import lenny.core.api as api
    from fastapi.testclient import TestClient
    from lenny import configs
    with patch.object(api, "PROXY", proxy), patch.object(api, "SCHEME", scheme), patch.object(api, "HOST", host), \
         patch.object(api, "PORT", port), patch.object(configs, "PROXY", proxy):
        return TestClient(app_module, base_url=base, headers={**headers, **(extra or {})}).get(URL)


def test_links_come_from_the_configured_domain(app_module, headers):
    r = links(app_module, headers, proxy="https://lender.briet.app")
    body = r.json()
    assert r.status_code == 200 and body["source"] == "configured" and body["is_public"] is True
    assert body["base_url"] == "https://lender.briet.app"
    assert body["opds_feed"] == "https://lender.briet.app/v1/api/opds"
    assert body["opds_book_template"] == "https://lender.briet.app/v1/api/opds/{olid}"
    assert body["opds_search"] == "https://lender.briet.app/v1/api/opds/search?query={query}"
    assert body["reader_url"] == "https://lender.briet.app/"       # the built-in reader, at the node root


def test_without_a_domain_the_host_and_port_from_the_setup_are_used(app_module, headers):
    body = links(app_module, headers, scheme="https", host="books.example.org", port=8443).json()
    assert body["source"] == "host_and_port" and body["base_url"] == "https://books.example.org:8443" and body["is_public"] is True
    body = links(app_module, headers, scheme="http", host="localhost", port=8080).json()
    assert body["base_url"] == "http://localhost:8080" and body["is_public"] is False                           # only works on this machine
    assert links(app_module, headers, scheme="https", host="books.example.org", port=443).json()["base_url"] == "https://books.example.org"


def test_the_request_never_changes_the_links(app_module, headers):
    """Host and forwarding headers are the caller's to set: they must not steer a link people will share."""
    extra = {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "evil.example.com", "Host": "evil.example.com"}
    body = links(app_module, headers, proxy="https://lender.briet.app", extra=extra, base="http://evil.example.com").json()
    assert body["base_url"] == "https://lender.briet.app" and "evil" not in body["reader_url"]


@pytest.mark.parametrize("proxy", ["http://localhost:8097", "http://127.0.0.1:8080", "http://192.168.1.20", "http://10.0.0.5:1337",
                                   "http://raspberrypi.local", "http://lenny", "http://[::1]:8080"])
def test_addresses_that_only_work_on_this_network_are_flagged(app_module, headers, proxy):
    assert links(app_module, headers, proxy=proxy).json()["is_public"] is False


@pytest.mark.parametrize("proxy", ["https://lender.briet.app", "https://8.8.8.8", "https://my-lib.trycloudflare.com"])
def test_public_addresses_are_not_flagged(app_module, headers, proxy):
    assert links(app_module, headers, proxy=proxy).json()["is_public"] is True


def test_links_require_admin(app_module):
    from fastapi.testclient import TestClient
    assert TestClient(app_module).get(URL).status_code == 403
