"""A client must not be able to choose the IP Lenny binds to (#210).

Lenny's nginx sets the header with `$proxy_add_x_forwarded_for`, which APPENDS
the address it observed to whatever the client already sent:

    client sends:  X-Forwarded-For: 1.2.3.4
    app receives:  X-Forwarded-For: 1.2.3.4, <address nginx observed>

uvicorn's ProxyHeadersMiddleware resolves that list against `forwarded_allow_ips`
(uvicorn/middleware/proxy_headers.py):

    if self.always_trust:                      # forwarded_allow_ips == "*"
        return x_forwarded_for_hosts[0]        # LEFTMOST -> the client's value
    for host in reversed(x_forwarded_for_hosts):
        if not self.trust_host(host):
            return host                        # rightmost untrusted -> real

So `'*'` hands the caller control of `request.client.host`, and every IP-based
check in Lenny — session-cookie binding, the `ip` baked into an OTP's HMAC —
becomes advisory rather than enforced.

These tests drive the real middleware rather than asserting on our config
string, so they fail for the same reason production would.
"""

import os

import pytest

# Set TESTING before any lenny imports
os.environ["TESTING"] = "true"

pytest.importorskip("uvicorn.middleware.proxy_headers")

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware  # noqa: E402

# The address nginx observed for the real caller — appended by
# $proxy_add_x_forwarded_for, so it is the RIGHTMOST entry.
REAL_CLIENT = "203.0.113.9"
# What a malicious caller prepends, hoping to be believed.
SPOOFED = "1.2.3.4"
# The in-network peer uvicorn actually accepts the connection from (nginx).
NGINX_PEER = "172.18.0.5"


async def _app(scope, receive, send):
    """Records the client the middleware resolved."""
    scope.setdefault("_seen", []).append(scope["client"])


def _scope(xff: str, peer: str = NGINX_PEER) -> dict:
    return {
        "type": "http",
        "scheme": "http",
        "headers": [(b"x-forwarded-for", xff.encode())],
        "client": (peer, 51234),
    }


async def _resolve(trusted, xff: str, peer: str = NGINX_PEER) -> str:
    """Run the real middleware and return the client host it settled on."""
    scope = _scope(xff, peer)
    await ProxyHeadersMiddleware(_app, trusted_hosts=trusted)(scope, None, None)
    return scope["client"][0]


@pytest.mark.anyio
async def test_default_rejects_a_spoofed_forwarded_for():
    """The whole point of #210. With Lenny's default, a client-supplied value
    must lose to the address nginx actually observed."""
    from lenny.configs import FORWARDED_ALLOW_IPS

    got = await _resolve(FORWARDED_ALLOW_IPS, f"{SPOOFED}, {REAL_CLIENT}")
    assert got == REAL_CLIENT
    assert got != SPOOFED, "a caller must not be able to choose its own IP"


@pytest.mark.anyio
async def test_wildcard_is_why_this_was_broken():
    """Characterization of the old default, so the regression is legible: '*'
    hands the caller exactly what it asked for."""
    got = await _resolve("*", f"{SPOOFED}, {REAL_CLIENT}")
    assert got == SPOOFED


@pytest.mark.anyio
async def test_default_still_resolves_the_real_patron_ip():
    """The fix must not undo #201. With no spoofing, the patron's own address
    still has to come through — otherwise every patron collapses onto the nginx
    container and shares one rate-limit bucket and one session binding."""
    from lenny.configs import FORWARDED_ALLOW_IPS

    assert await _resolve(FORWARDED_ALLOW_IPS, REAL_CLIENT) == REAL_CLIENT


@pytest.mark.anyio
async def test_nginx_proxy_add_shape_yields_the_real_patron_ip():
    """The exact regression that already shipped once (#201's motivation).

    Reproduces what nginx's `$proxy_add_x_forwarded_for` actually builds for an
    ordinary patron with no prior header, from a peer inside the Compose
    network, and asserts the patron's own address comes through — not the
    proxy's. If this fails, every patron shares one OTP rate-limit bucket.
    """
    from lenny.configs import FORWARDED_ALLOW_IPS

    got = await _resolve(FORWARDED_ALLOW_IPS, REAL_CLIENT, peer=NGINX_PEER)
    assert got == REAL_CLIENT
    assert got != NGINX_PEER, "patrons collapsed onto the proxy address"


@pytest.mark.anyio
async def test_multiple_spoofed_hops_all_lose():
    """A caller can send a whole fake chain; it is still discarded."""
    from lenny.configs import FORWARDED_ALLOW_IPS

    xff = f"{SPOOFED}, 5.6.7.8, 9.10.11.12, {REAL_CLIENT}"
    assert await _resolve(FORWARDED_ALLOW_IPS, xff) == REAL_CLIENT


@pytest.mark.anyio
async def test_empty_value_collapses_every_patron_onto_the_proxy():
    """Documents why an empty value is worse than the bug it replaces: uvicorn
    trusts no proxy, so the connecting peer wins and every patron shares one
    address. The config layer must therefore never produce an empty string."""
    assert await _resolve("", f"{SPOOFED}, {REAL_CLIENT}") == NGINX_PEER


# ---------------------------------------------------------------------------
# Config layer: the default must be safe, and must never be empty
# ---------------------------------------------------------------------------

def test_config_default_is_not_wildcard():
    from lenny.configs import FORWARDED_ALLOW_IPS

    assert FORWARDED_ALLOW_IPS != "*"
    assert FORWARDED_ALLOW_IPS


def test_uvicorn_options_use_the_bounded_value():
    from lenny.configs import FORWARDED_ALLOW_IPS, OPTIONS

    assert OPTIONS["proxy_headers"] is True
    assert OPTIONS["forwarded_allow_ips"] == FORWARDED_ALLOW_IPS
    assert OPTIONS["forwarded_allow_ips"] != "*"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_env_var_falls_back_rather_than_disabling_proxy_trust(blank, monkeypatch):
    """An operator who sets the variable to nothing (or a script whose subnet
    lookup returned empty) must not silently disable proxy trust."""
    import importlib

    monkeypatch.setenv("LENNY_FORWARDED_ALLOW_IPS", blank)
    import lenny.configs as configs

    reloaded = importlib.reload(configs)
    try:
        assert reloaded.FORWARDED_ALLOW_IPS.strip()
        assert reloaded.FORWARDED_ALLOW_IPS != "*"
    finally:
        monkeypatch.delenv("LENNY_FORWARDED_ALLOW_IPS", raising=False)
        importlib.reload(configs)


def test_operator_override_is_honoured(monkeypatch):
    import importlib

    monkeypatch.setenv("LENNY_FORWARDED_ALLOW_IPS", "10.1.2.0/24")
    import lenny.configs as configs

    reloaded = importlib.reload(configs)
    try:
        assert reloaded.FORWARDED_ALLOW_IPS == "10.1.2.0/24"
    finally:
        monkeypatch.delenv("LENNY_FORWARDED_ALLOW_IPS", raising=False)
        importlib.reload(configs)


# ---------------------------------------------------------------------------
# The two shipped entrypoints must not drift apart
# ---------------------------------------------------------------------------

def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1]


def test_container_entrypoint_has_no_wildcard_default():
    """configs.OPTIONS only covers `python -m lenny.app`. Production starts
    uvicorn from the Dockerfile CMD, which carries its own default."""
    cmd = (_repo_root() / "docker" / "api" / "Dockerfile").read_text()
    assert "--forwarded-allow-ips" in cmd
    assert "LENNY_FORWARDED_ALLOW_IPS:-*" not in cmd, "wildcard default in the container entrypoint"
    assert "LENNY_FORWARDED_ALLOW_IPS:-}" not in cmd, "empty default trusts no proxy"


# ---------------------------------------------------------------------------
# Middleware ordering: the warning must observe the RESOLVED client
# ---------------------------------------------------------------------------

def _client_through_proxy(trusted, peer: str = NGINX_PEER):
    """Drive the real app the way uvicorn does.

    `uvicorn.config.Config.load` wraps the loaded app as
    `ProxyHeadersMiddleware(self.loaded_app, trusted_hosts=...)`, i.e. OUTSIDE
    every Starlette/FastAPI middleware. So the app's own middleware sees a
    `scope["client"]` that has already been resolved. This pins that assumption
    rather than trusting it — if uvicorn ever moved the wrap inside, the warning
    would silently start reading the raw peer and fire on every request.
    """
    from unittest.mock import patch

    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    with patch("lenny.core.db.init"), patch("lenny.core.db.create_engine"):
        from lenny.app import app

        proxied = ProxyHeadersMiddleware(app, trusted_hosts=trusted)

        # TestClient's peer is the literal string "testclient", which is in no
        # trusted range — ProxyHeadersMiddleware would ignore the header outright
        # and the test would measure nothing. Rewrite the peer OUTSIDE the
        # middleware so it sees a connection arriving from inside the Compose
        # network, the way nginx's actually does.
        async def with_peer(scope, receive, send):
            if scope["type"] == "http":
                scope = {**scope, "client": (peer, 51234)}
            await proxied(scope, receive, send)

        return TestClient(with_peer)


def _reset_warned():
    import lenny.app as lenny_app

    lenny_app._xff_warned = False
    return lenny_app


def test_no_warning_when_the_chain_resolves_correctly(caplog):
    """A correctly configured instance must stay quiet, or the signal is noise."""
    import logging

    from lenny.configs import FORWARDED_ALLOW_IPS

    lenny_app = _reset_warned()
    client = _client_through_proxy(FORWARDED_ALLOW_IPS)
    with caplog.at_level(logging.WARNING, logger="lenny.app"):
        client.get("/v1/api/health", headers={"x-forwarded-for": f"{SPOOFED}, {REAL_CLIENT}"})

    assert "X-Forwarded-For is not being resolved" not in caplog.text
    assert lenny_app._xff_warned is False


def test_warns_when_the_range_is_too_broad(caplog):
    """'*' resolves to the first hop — the caller's own value. Must be loud."""
    import logging

    lenny_app = _reset_warned()
    client = _client_through_proxy("*")
    with caplog.at_level(logging.WARNING, logger="lenny.app"):
        client.get("/v1/api/health", headers={"x-forwarded-for": f"{SPOOFED}, {REAL_CLIENT}"})

    assert "X-Forwarded-For is not being resolved" in caplog.text
    assert lenny_app._xff_warned is True


def test_warns_when_the_range_is_too_narrow(caplog):
    """Trusting nothing collapses every patron onto the proxy. Also must be loud."""
    import logging

    lenny_app = _reset_warned()
    client = _client_through_proxy("10.255.255.255")
    with caplog.at_level(logging.WARNING, logger="lenny.app"):
        client.get("/v1/api/health", headers={"x-forwarded-for": f"{SPOOFED}, {REAL_CLIENT}"})

    assert "X-Forwarded-For is not being resolved" in caplog.text


def test_warning_fires_only_once_per_process(caplog):
    """Module-level flag: one warning per worker process, not per request.

    The container runs `--workers=3` by default, so an operator may see up to
    three. That is intended — it is a startup-class diagnostic, not a per-request
    log — and this test exists so a reviewer does not read it as a bug.
    """
    import logging

    _reset_warned()
    client = _client_through_proxy("*")
    with caplog.at_level(logging.WARNING, logger="lenny.app"):
        for _ in range(5):
            client.get("/v1/api/health",
                       headers={"x-forwarded-for": f"{SPOOFED}, {REAL_CLIENT}"})

    assert caplog.text.count("X-Forwarded-For is not being resolved") == 1


def test_untrusted_peer_cannot_spoof_at_all(caplog):
    """A caller reaching uvicorn directly (not via nginx) has its header ignored
    outright — the peer wins. Port 1337 is never published, so this is defence
    in depth, but it is the property that makes the bounded range meaningful."""
    import logging

    from lenny.configs import FORWARDED_ALLOW_IPS

    _reset_warned()
    outsider = "198.51.100.77"
    client = _client_through_proxy(FORWARDED_ALLOW_IPS, peer=outsider)
    with caplog.at_level(logging.WARNING, logger="lenny.app"):
        resp = client.get("/v1/api/health",
                          headers={"x-forwarded-for": f"{SPOOFED}, {REAL_CLIENT}"})

    assert resp.status_code == 200
    # The warning is correct here: the chain genuinely was not honoured.
    assert "X-Forwarded-For is not being resolved" in caplog.text


def test_no_warning_without_a_forwarded_header(caplog):
    """Direct requests (health checks, container probes) must not warn."""
    import logging

    from lenny.configs import FORWARDED_ALLOW_IPS

    _reset_warned()
    client = _client_through_proxy(FORWARDED_ALLOW_IPS)
    with caplog.at_level(logging.WARNING, logger="lenny.app"):
        client.get("/v1/api/health")

    assert "X-Forwarded-For is not being resolved" not in caplog.text


def test_configure_templates_the_variable_into_env():
    """A fresh install must get an explicit value written to .env, not inherit
    whatever the entrypoint falls back to."""
    script = (_repo_root() / "docker" / "configure.sh").read_text()
    assert "LENNY_FORWARDED_ALLOW_IPS=$LENNY_FORWARDED_ALLOW_IPS" in script
    assert 'LENNY_FORWARDED_ALLOW_IPS="${LENNY_FORWARDED_ALLOW_IPS:-' in script
