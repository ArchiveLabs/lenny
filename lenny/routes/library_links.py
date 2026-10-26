#!/usr/bin/env python

"""
Links an admin can copy and share for this library.

    GET /admin/library/links

The OPDS feed (what reading apps subscribe to), a per-book OPDS address, and the
address of this node's built-in OPDS reader (its own root).

They are built from the address the owner configured for this Lenny, the same one the OPDS
feed and the OAuth metadata advertise (`LENNY_PROXY` in .env, else `LENNY_HOST`/`LENNY_PORT`).
Never from the request, so nothing about them is guessed from how the admin happened to connect.
"""

import ipaddress
from urllib.parse import urlsplit

from fastapi import APIRouter, Request

from lenny.core.api import LennyAPI
from lenny.routes.api import _require_admin

router = APIRouter()

OPDS_PATH = "/v1/api/opds"


def _is_private(host: str) -> bool:
    """True for an address only this machine or its network can reach (localhost, 192.168.x.x, .local)."""
    host = (host or "").split(":")[0].strip("[]").lower() if not (host or "").startswith("[") else host.split("]")[0].strip("[").lower()
    if host in ("", "localhost") or host.endswith((".local", ".internal", ".lan")) or ("." not in host and ":" not in host):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


@router.get("/admin/library/links")
async def library_links(request: Request):
    """Copyable links for this library: `opds_feed`, `opds_search` (replace `{query}`), `opds_book_template`
    (replace `{olid}` with a book's edition number), `reader_url` (this node's built-in reader, served at its
    root, `<base_url>/`), plus `base_url`, `source` and `is_public`.
    `source` is `configured` when LENNY_PROXY is set, else `host_and_port`. `is_public` false means the address
    only works on this machine or network: set LENNY_PROXY to the domain or tunnel (for example
    `https://lender.briet.app`) for links that work for others."""
    _require_admin(request)
    from lenny import configs
    base = LennyAPI.make_url("").rstrip("/")
    feed = f"{base}{OPDS_PATH}"
    return {
        "base_url": base, "source": "configured" if configs.PROXY else "host_and_port",
        "is_public": not _is_private(urlsplit(base).netloc),
        "opds_feed": feed, "opds_search": f"{feed}/search?query={{query}}", "opds_book_template": f"{feed}/{{olid}}",
        "reader_url": f"{base}/",
    }
