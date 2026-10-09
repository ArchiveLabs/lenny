"""The OPDS Authentication Document follows the admin's active auth mode (#232).

One flow at a time: `external` advertises Authorization Code + PKCE only, every
other mode keeps the legacy implicit entry untouched. Advertising both waits on
the revised spec (#237).
"""
import pytest
from fastapi.testclient import TestClient
from pyopds2_lenny import LennyDataProvider

import lenny.core.api as core_api
from lenny.app import app

IMPLICIT = "http://opds-spec.org/auth/oauth/implicit"
PKCE = "http://opds-spec.org/auth/oauth/authorization-code-with-pkce"


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value):
        monkeypatch.setattr(core_api._configs, "read_lending_mode", lambda: value)
    return set_mode


def types(doc):
    return [a["type"] for a in doc["authentication"]]


@pytest.mark.parametrize("m", ["none", "ol", "", "something-new"])
def test_non_external_modes_show_implicit_only(mode, m):
    mode(m)
    assert types(core_api.auth_document()) == [IMPLICIT]


def test_external_mode_shows_pkce_only(mode):
    mode("external")
    doc = core_api.auth_document()
    assert types(doc) == [PKCE]
    issuer = LennyDataProvider.OAUTH_ISSUER
    assert issuer and not issuer.endswith("/")
    links = {l["rel"]: l["href"] for l in doc["authentication"][0]["links"]}
    assert links["authenticate"] == f"{issuer}/v1/api/oauth2/authorize"
    assert links["code"] == links["refresh"] == f"{issuer}/v1/api/oauth2/token"


def test_implicit_mode_is_what_the_library_produced_before(mode, monkeypatch):
    """Existing clients (the demo reader) must see the same document as before."""
    mode("ol")
    monkeypatch.setattr(LennyDataProvider, "OAUTH_ISSUER", "")
    legacy = LennyDataProvider.get_authentication_document()
    monkeypatch.undo()
    mode("ol")
    assert core_api.auth_document() == legacy


def test_library_without_pkce_entry_never_yields_an_empty_document(mode, monkeypatch):
    mode("external")
    monkeypatch.setattr(LennyDataProvider, "OAUTH_ISSUER", "")
    assert types(core_api.auth_document()) == [IMPLICIT]


@pytest.mark.parametrize("m", ["ol", "none", "external"])
def test_both_mode_advertises_both_flows_in_every_admin_mode(mode, monkeypatch, m):
    """The upgrade escape hatch: a reader that only speaks implicit keeps working."""
    mode(m)
    monkeypatch.setenv("LENNY_AUTH_DOC_MODE", "both")
    assert types(core_api.auth_document()) == [IMPLICIT, PKCE]


@pytest.mark.parametrize("value", ["", "active", "ACTIVE", "nonsense"])
def test_anything_but_both_keeps_one_flow_at_a_time(mode, monkeypatch, value):
    mode("external")
    monkeypatch.setenv("LENNY_AUTH_DOC_MODE", value)
    assert types(core_api.auth_document()) == [PKCE]


def test_switching_changes_the_document_live(mode):
    seen = []
    for m in ["ol", "external", "none", "external", "ol"]:
        mode(m)
        seen.append(types(core_api.auth_document()))
    assert seen == [[IMPLICIT], [PKCE], [IMPLICIT], [PKCE], [IMPLICIT]]


@pytest.mark.parametrize("m,want", [("ol", IMPLICIT), ("none", IMPLICIT), ("external", PKCE)])
def test_route_and_401_bodies_agree_with_the_mode(mode, m, want):
    mode(m)
    c = TestClient(app)
    doc_route = c.get("/v1/api/oauth/implicit")
    assert doc_route.status_code == 200
    assert doc_route.headers["content-type"].startswith("application/opds-authentication+json")
    assert types(doc_route.json()) == [want]
    # Protected routes send the same document in their 401 body.
    r = c.get("/v1/api/shelf")
    assert r.status_code == 401
    assert types(r.json()) == [want]


def test_pkce_links_match_oauth_server_metadata(mode):
    mode("external")
    c = TestClient(app)
    meta = c.get("/.well-known/oauth-authorization-server").json()
    links = {l["rel"]: l["href"] for l in c.get("/v1/api/oauth/implicit").json()["authentication"][0]["links"]}
    assert links["authenticate"] == meta["authorization_endpoint"]
    assert links["code"] == links["refresh"] == meta["token_endpoint"]
    assert "S256" in meta["code_challenge_methods_supported"]


def test_document_id_never_names_a_flow_it_does_not_advertise(mode):
    mode("external")
    pkce = core_api.auth_document()
    assert pkce["id"].endswith("/v1/api/oauth/authentication")
    assert "implicit" not in pkce["id"]
    mode("ol")
    # An implicit document keeps the id existing readers stored.
    assert core_api.auth_document()["id"].endswith("/v1/api/oauth/implicit")


@pytest.mark.parametrize("path", ["/v1/api/oauth/implicit", "/v1/api/oauth/authentication"])
def test_the_id_is_an_address_that_serves_the_document(mode, path):
    mode("external")
    c = TestClient(app)
    served = c.get(path)
    assert served.status_code == 200
    assert served.json() == core_api.auth_document()
    assert served.json()["id"].endswith("/v1/api/oauth/authentication")
    assert c.get(served.json()["id"].replace(LennyDataProvider.BASE_URL, "/v1/api/")).status_code == 200
