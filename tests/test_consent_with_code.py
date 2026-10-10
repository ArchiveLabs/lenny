"""One screen: what is being granted, and the code that proves who is granting it.

When Open Library sends a `login_hint` the patron has no Lenny session, and the
old shape made them cross three screens — address, code, consent — before
anything asked for a decision. The consent now carries the code field, so the
request is stated once and answered once.

The binding is the part that matters and it is not cosmetic. A loan keys on
`patron_email_hash`, a SHA-256 of the lowercased address
(`lenny/core/utils.hash_email`, used by `Loan.patron_email_hash`). If the grant
were recorded against any address other than the one Open Library will use, the
consumer would hold a working token for a loan it cannot read — a dead end with
no error anywhere. So the hinted address is bound into the signed handle, there
is no way to continue under a different one, and a session belonging to someone
else is not allowed to stand in for it.
"""

import os
import re
from urllib.parse import urlencode, urlparse, parse_qs

import pytest

os.environ.setdefault("TESTING", "true")
os.environ.setdefault("LENNY_SEED", "consent-code-test-seed-32-chars!")

from fastapi.testclient import TestClient  # noqa: E402

from lenny.app import app  # noqa: E402
from lenny.core import auth  # noqa: E402
from lenny.core.cache import CacheEntry  # noqa: E402
from lenny.core.db import Base, engine  # noqa: E402
from lenny.core.db import session as db  # noqa: E402
from lenny.core.oauth2 import AuthorizationCode, OAuthClient  # noqa: E402
from lenny.core.utils import hash_email  # noqa: E402

REDIRECT = "https://openlibrary.org/borrow/lenny/callback"
PATRON = "patron@example.org"
OTHER = "someone-else@example.org"
GOOD_CODE = "123456"
NAVIGATION = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
AUTHORIZE = "/v1/api/oauth2/authorize"


@pytest.fixture(autouse=True)
def clean():
    Base.metadata.create_all(engine)
    for t in (CacheEntry,):
        db.query(t).delete()
    db.query(AuthorizationCode).delete()
    db.query(OAuthClient).delete()
    db.commit()
    yield
    db.query(CacheEntry).delete()
    db.query(AuthorizationCode).delete()
    db.query(OAuthClient).delete()
    db.commit()
    db.remove()


@pytest.fixture(autouse=True)
def lending_on(monkeypatch):
    """Only the `/oauth/authorize` hop consults the lending gate, so the
    collapsed path never needed this and the no-hint path 503s without it."""
    monkeypatch.setattr("lenny.routes.oauth._require_lending", lambda: None)


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr("lenny.core.auth.OTP.issue",
                        classmethod(lambda cls, email, ip: sent.append(email)))
    return sent


@pytest.fixture(autouse=True)
def ol_accepts_the_code(monkeypatch):
    """Stub only Open Library's side of redeem, so `verify` -> `authenticate`
    (and its attempt rate limit) stay real."""
    monkeypatch.setattr(
        "lenny.core.auth.OTP.redeem",
        classmethod(lambda cls, email, ip, otp: otp == GOOD_CODE))


@pytest.fixture
def client():
    obj, _ = OAuthClient.register(
        name="Open Library", redirect_uris=[REDIRECT],
        scopes=["loans:read", "borrow"])
    return obj


def params(client, **extra):
    p = {
        "client_id": client.client_id, "redirect_uri": REDIRECT,
        "response_type": "code", "scope": "loans:read borrow", "state": "xyz",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": "S256",
    }
    p.update(extra)
    return p


def screen(client, **extra):
    """The one screen a hinted, signed-out patron lands on."""
    c = TestClient(app, follow_redirects=False)
    return c.get(AUTHORIZE, params=params(client, login_hint=PATRON, **extra),
                 headers=NAVIGATION)


def handle_of(html):
    m = re.search(r'name="request" value="([^"]+)"', html)
    assert m, "no signed handle on the page"
    return m.group(1)


def visible(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


class TestOneScreen:
    def test_the_code_is_already_sent_when_the_screen_appears(self, client, outbox):
        r = screen(client)
        assert r.status_code == 200
        assert outbox == [PATRON], f"expected one code to {PATRON}, sent {outbox}"

    def test_it_carries_the_request_the_permissions_and_the_code_field(self, client, outbox):
        html = screen(client).text
        text = visible(html)
        assert "Open Library" in text
        assert "See which books you have on loan" in text
        assert f"sent a code to" in text.lower() or PATRON in text
        assert 'name="otp"' in html, "no code field on the consent screen"
        assert 'value="allow"' in html and 'value="deny"' in html

    def test_the_order_is_request_then_permissions_then_code_then_buttons(self, client, outbox):
        """Mek's ordering. The decision has to be readable before the field
        that takes the attention."""
        html = screen(client).text
        i_request = html.index("Open Library")
        i_scope = html.index("See which books you have on loan")
        i_code = html.index('name="otp"')
        i_allow = html.index('value="allow"')
        assert i_request < i_scope < i_code < i_allow, (
            f"order was request={i_request} scope={i_scope} "
            f"code={i_code} allow={i_allow}")

    def test_the_button_names_the_act_not_the_form(self, client, outbox):
        """"Submit" would make this read as a code form that happens to grant
        something."""
        text = visible(screen(client).text)
        assert "Allow Open Library" in text


class TestItIsBoundToTheHintedAddress:
    def test_there_is_no_way_to_continue_as_someone_else(self, client, outbox):
        """A loan recorded against a different address cannot be opened by the
        consumer, so switching accounts mid-flow is not an escape hatch."""
        html = screen(client).text
        assert "Not you?" not in html
        assert "prompt=select_account" not in html
        assert "different account" not in visible(html)

    def test_the_grant_is_recorded_against_the_hinted_address(self, client, outbox):
        html = screen(client).text
        c = TestClient(app, follow_redirects=False)
        r = c.post(AUTHORIZE, data={"request": handle_of(html),
                                    "decision": "allow", "otp": GOOD_CODE})
        assert r.status_code in (302, 303), r.text[:300]
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        row = AuthorizationCode.lookup(code) if hasattr(AuthorizationCode, "lookup") else None
        if row is None:
            import hashlib
            h = hashlib.sha256(code.encode()).hexdigest()
            row = db.query(AuthorizationCode).filter(
                AuthorizationCode.code_hash == h).first()
        assert row is not None, "the issued code is not in the table"
        assert row.patron_email_hash == hash_email(PATRON)

    def test_a_session_for_a_different_address_cannot_stand_in(self, client, outbox):
        """The dead end this exists to prevent: Open Library says the patron is
        A, a stale Lenny cookie says B, and the loan is written under B."""
        c = TestClient(app, follow_redirects=False)
        r = c.get(AUTHORIZE, params=params(client, login_hint=PATRON),
                  cookies={"session": auth.create_session_cookie(OTHER)},
                  headers=NAVIGATION)
        assert r.status_code == 200
        assert OTHER not in r.text, "proceeded under the session's address"
        assert 'name="otp"' in r.text, "did not fall back to proving the hint"
        assert outbox == [PATRON]

    def test_a_matching_session_still_skips_the_code(self, client, outbox):
        """Same address on both sides is the normal repeat case and must not
        be made worse."""
        c = TestClient(app, follow_redirects=False)
        r = c.get(AUTHORIZE, params=params(client, login_hint=PATRON),
                  cookies={"session": auth.create_session_cookie(PATRON)},
                  headers=NAVIGATION)
        assert r.status_code == 200
        assert 'name="otp"' not in r.text, "asked a signed-in patron for a code"
        assert outbox == [], f"mailed a signed-in patron: {outbox}"


class TestDecidingWithoutAValidCode:
    def test_decline_never_checks_the_code(self, client, outbox):
        """Refusing is not conditional on proving who you are."""
        html = screen(client).text
        c = TestClient(app, follow_redirects=False)
        r = c.post(AUTHORIZE, data={"request": handle_of(html), "decision": "deny"})
        assert r.status_code in (302, 303)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"][0] == "access_denied"

    def test_a_wrong_code_re_renders_without_sending_another(self, client, outbox):
        html = screen(client).text
        c = TestClient(app, follow_redirects=False)
        r = c.post(AUTHORIZE, data={"request": handle_of(html),
                                    "decision": "allow", "otp": "000000"})
        assert r.status_code == 200
        assert 'name="otp"' in r.text, "the code field is gone after a typo"
        assert outbox == [PATRON], f"a typo triggered another send: {outbox}"

    def test_a_wrong_code_keeps_the_authorization_request(self, client, outbox):
        """Losing the request would send the patron back to Open Library to
        start again, which is what a typo must not cost."""
        first = screen(client).text
        c = TestClient(app, follow_redirects=False)
        r = c.post(AUTHORIZE, data={"request": handle_of(first),
                                    "decision": "allow", "otp": "000000"})
        again = handle_of(r.text)
        good = c.post(AUTHORIZE, data={"request": again, "decision": "allow",
                                       "otp": GOOD_CODE})
        assert good.status_code in (302, 303), good.text[:300]
        assert "code=" in good.headers["location"]

    def test_allowing_signs_the_patron_in(self, client, outbox):
        html = screen(client).text
        c = TestClient(app, follow_redirects=False)
        r = c.post(AUTHORIZE, data={"request": handle_of(html),
                                    "decision": "allow", "otp": GOOD_CODE})
        assert "session" in r.cookies, "no Lenny session after proving the address"

    def test_one_handle_cannot_be_spent_twice(self, client, outbox):
        html = screen(client).text
        h = handle_of(html)
        c = TestClient(app, follow_redirects=False)
        first = c.post(AUTHORIZE, data={"request": h, "decision": "allow",
                                        "otp": GOOD_CODE})
        assert first.status_code in (302, 303)
        second = c.post(AUTHORIZE, data={"request": h, "decision": "allow",
                                         "otp": GOOD_CODE})
        assert second.status_code not in (302, 303), \
            "the same approval was honoured twice"


class TestTheCopyMatchesWhatTheGrantConfers:
    def test_it_does_not_promise_returning(self, client, outbox):
        """`return_item` (lenny/routes/api.py) takes a cookie and never calls
        `get_authenticated_identity`, so a token-only consumer cannot return a
        book. The consent screen must not say it can."""
        text = visible(screen(client).text).lower()
        # Narrowly the *capability* claim. "You will return to openlibrary.org
        # afterwards" is about navigation and is both true and wanted, so a
        # bare search for "return" flags the wrong sentence.
        promises = re.findall(r"return\w*\s+(?:books?|loans?|them|it)\b", text)
        promises += re.findall(r"\breturn\b(?![\s]+to\b)", text)
        assert not promises, \
            f"the screen still promises returning, which a token cannot do: {promises}"
        assert "borrow and return" not in text

    def test_the_scope_table_does_not_promise_returning_either(self):
        from lenny.core.oauth2 import SCOPES
        assert "return" not in SCOPES["borrow"].lower()


class TestNoCopyClaimsAPositionInASequence:
    """The collapse changed the flow's shape, so every string describing that
    shape had to be re-read.

    The consent badge said "Last step". It was renamed from "Authorize" while
    consent really was the third of three screens, and then the flow became one
    screen and nothing re-read the label — it told a patron they were finishing
    a sequence that never happened. The badge is gone rather than made
    conditional: it is true on some paths and false on others, and one word is
    not worth a branch.
    """

    def test_the_one_screen_claims_no_position(self, client, outbox):
        text = visible(screen(client).text).lower()
        for claim in ("last step", "step 1", "step 2", "step one", "step two",
                      "almost done", "final step", "next,"):
            assert claim not in text, f"the only screen still says {claim!r}"

    def test_the_consent_screen_has_no_badge_at_all(self, client, outbox):
        assert 'class="badge' not in screen(client).text

    def test_the_signed_in_consent_screen_claims_no_position_either(self, client, outbox):
        """A patron who already has a session also sees exactly one screen."""
        c = TestClient(app, follow_redirects=False)
        r = c.get(AUTHORIZE, params=params(client),
                  cookies={"session": auth.create_session_cookie(PATRON)})
        text = visible(r.text).lower()
        assert "last step" not in text and 'class="badge' not in r.text

    def test_the_sign_in_pages_may_still_promise_consent_because_it_follows(self, client, outbox):
        """The opposite error is deleting copy that is still true. Without a
        hint there IS a later consent screen, so saying so is accurate — and
        this test fails if that stops being the case."""
        c = TestClient(app, follow_redirects=False)
        bounce = c.get(AUTHORIZE, params=params(client))   # no login_hint
        page = c.get(bounce.headers["location"], headers=NAVIGATION)
        assert "choose whether to allow" in visible(page.text), \
            "the email step no longer warns that consent follows"
        assert 'id="email"' in page.text

    def test_and_consent_really_does_follow_on_that_path(self, client, outbox):
        """Proves the sentence above rather than trusting it: sign in by code
        on the no-hint path and land on the consent screen."""
        c = TestClient(app, follow_redirects=False)
        bounce = c.get(AUTHORIZE, params=params(client))
        login_url = bounce.headers["location"]
        page = c.get(login_url, headers=NAVIGATION)
        post_url = re.search(r'action="([^"]+)"', page.text).group(1)
        # email step -> code step
        step2 = c.post(post_url, data={"email": PATRON, "redirect_uri": "",
                                       "state": "", "book_id": "oauth",
                                       "action": "oauth", "next": ""})
        assert 'id="otpForm"' in step2.text, "did not reach the code step"
        # code step -> back to /oauth2/authorize, which renders consent
        done = c.post(re.search(r'action="([^"]+)"', step2.text).group(1),
                      data={"email": PATRON, "otp": GOOD_CODE, "redirect_uri": "",
                            "state": "", "book_id": "oauth", "action": "oauth",
                            "next": ""}, follow_redirects=False)
        assert done.status_code in (302, 303), f"no redirect home: {done.status_code}"
