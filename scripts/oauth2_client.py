#!/usr/bin/env python3
"""Operator commands for OAuth 2.0 clients.

Registration is open by default, so an operator eventually needs to see who has
registered and stop one. Without this the only remedy is an ORM call in a Python
console, which is not a thing anyone should have to do to cut off an abusive
consumer.

    make ol-connect          # the common case: let Open Library act for patrons
    make ol-disconnect
    make oauth2-register NAME="…" URI=https://…/callback
    make oauth2-clients
    make oauth2-disable CLIENT=<client_id>
    make oauth2-sweep

Registration is deliberately an operator action rather than an open endpoint: a
library decides who may act on its patrons' behalf, and for this ecosystem the
consumers are a known, curated set rather than a long tail. See #209.
"""

import argparse
import os
import re
import sys
from urllib.parse import urlparse

from lenny.core.db import session as db
from lenny.core.oauth2 import SCOPES, OAuthClient, sweep_expired


# Open Library is the consumer nearly every node wants, so it gets a named
# command rather than an incantation the operator has to remember. The redirect
# is overridable because OL's callback path is still being built.
OPENLIBRARY_NAME = "Open Library"
OPENLIBRARY_REDIRECT = "https://openlibrary.org/lenny/callback"


def _openlibrary_client():
    """The Open Library client that is current, enabled or not, or None.

    Prefers a live registration, then the newest by id. Ordering by `created_at`
    would be ambiguous: two registrations made in the same second — which
    `--rotate` does by construction — can share a timestamp, and then "current"
    depends on the backend's clock resolution rather than on the facts.
    """
    rows = (db.query(OAuthClient)
            .filter(OAuthClient.name == OPENLIBRARY_NAME)
            .order_by(OAuthClient.id.desc())
            .all())
    live = [r for r in rows if r.disabled_at is None]
    return (live or rows or [None])[0]


def _node_credentials():
    """This node's own OL/IA S3 keys, or None.

    Set by `make ol-login`. They are how Open Library knows *which* Lenny node
    is calling — the node's identity, distinct from any patron's.
    """
    from lenny import configs
    if configs.OL_S3_ACCESS_KEY and configs.OL_S3_SECRET_KEY:
        return configs.OL_S3_ACCESS_KEY, configs.OL_S3_SECRET_KEY
    return None


def _provider_name() -> str:
    """This node's `provider_name` for Open Library's feed registry.

    Not cosmetic, and not a hostname. Open Library uses `provider_name` as both
    the `identifiers` key and the `source_records` prefix of every edition a
    feed touches, and its import validator only accepts a record when the two
    agree. A hostname would mint a dot-bearing identifier key —
    `identifiers: {"example.org": [...]}` — which is not the shape Open Library
    identifier keys take, and it would be permanent in edition data.

    So: a `lenny_` prefix, which lets Open Library recognise the family, plus
    the full host slugified. The TLD is kept deliberately, because
    `example.org` and `example.com` are different libraries.

    `OL_PROVIDER_NAME` overrides it, for a node whose name Open Library has
    already agreed on.
    """
    from lenny.core.api import LennyAPI

    if override := os.environ.get("OL_PROVIDER_NAME"):
        return override
    host = urlparse(LennyAPI.make_url("")).hostname or ""
    slug = re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_")
    return f"lenny_{slug}" if slug else "lenny"


def _register_with_openlibrary(client, secret, redirect_uri) -> tuple[bool, str]:
    """Tell Open Library this node exists and how to talk to it.

    Sends the feed to harvest and the OAuth credentials to use, authenticated
    as the node with its own S3 keys — the same `Authorization: LOW` header
    Open Library already parses everywhere else.

    Returns (ok, message). A failure here is not fatal: the node is fully
    configured either way, and an operator can retry or fall back to sending
    the credentials by hand.
    """
    import httpx

    from lenny import configs
    from lenny.core.api import LennyAPI
    from lenny.core.openlibrary import ol_auth_headers

    url = os.environ.get(
        "OL_FEED_REGISTRY_URL",
        f"{configs.OTP_SERVER.rstrip('/')}/api/feed-registry")
    payload = {
        "provider_name": _provider_name(),
        "feed_type": "opds",
        "url": LennyAPI.make_url("/v1/api/opds"),
        "id_strategy": "self_link",
        "oauth": {
            # The node's identity lives here, not in `provider_name`: that one
            # is a slug baked into edition data, this one is the address a
            # consumer actually talks to.
            "issuer": LennyAPI.make_url("").rstrip("/"),
            "client_id": client.client_id,
            "client_secret": secret,
        },
    }
    try:
        r = httpx.post(url, json=payload, headers=ol_auth_headers(), timeout=30)
    except httpx.HTTPError as exc:
        return False, f"could not reach Open Library at {url}: {exc}"
    if r.status_code in (200, 201):
        return True, f"registered with Open Library ({r.status_code})"
    if r.status_code == 404:
        return False, (f"Open Library has no feed-registry endpoint at {url} yet. "
                       "Send the credentials below by hand for now.")
    return False, f"Open Library refused the registration: HTTP {r.status_code} {r.text[:200]}"


def cmd_ol_connect(args) -> int:
    """Let Open Library act on patrons' behalf. Safe to run twice."""
    if _node_credentials() is None:
        print("This node has no Open Library credentials, so Open Library has no\n"
              "way to tell which Lenny is calling.\n\n"
              "  Run `make ol-login` first, signing in with the Internet Archive\n"
              "  account that represents this library. Any custodial account\n"
              "  registered for this instance will do — it identifies the node,\n"
              "  not a patron.\n", file=sys.stderr)
        return 1

    existing = _openlibrary_client()

    if existing and not existing.disabled_at and not args.rotate:
        print(f"Open Library is already connected ({existing.client_id}).")
        print("  Lost the secret? Re-run with --rotate to issue a new one.")
        print("  Disconnect with: make ol-disconnect")
        return 0

    if existing and existing.disabled_at and not args.rotate:
        existing.disabled_at = None
        db.add(existing)
        db.commit()
        print(f"Reconnected Open Library ({existing.client_id}).")
        print("  Its previous secret still works. Use --rotate to replace it.")
        return 0

    if existing and args.rotate:
        # Rotating means the old secret must stop working, and so must every
        # token issued under it — otherwise "rotate" would leave the thing you
        # were rotating away from still able to act.
        revoked = OAuthClient.disable(existing.client_id)
        print(f"Retired the previous registration, revoking {revoked} token(s).")

    try:
        client, secret = OAuthClient.register(
            name=OPENLIBRARY_NAME, redirect_uris=[args.redirect_uri],
            scopes=sorted(SCOPES))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    sent, detail = _register_with_openlibrary(client, secret, args.redirect_uri)

    print("Connected Open Library.\n")
    print(f"  client_id     {client.client_id}")
    print(f"  redirect_uri  {args.redirect_uri}")
    print(f"  scopes        {client.scopes}")

    if sent:
        print(f"\n  {detail} — it has this node's feed and credentials.")
        print("  Nothing further to send; the secret is not shown because it")
        print("  does not need to travel through a person.")
        return 0

    print(f"\n  client_secret {secret}")
    print(f"\n  Could not register automatically: {detail}")
    print("  Send the client_id and client_secret to Open Library by hand —")
    print("  only the hash is stored here, so this is the one time the secret")
    print("  can be read. Re-run with --rotate if you lose it.")
    return 0


def cmd_ol_disconnect(args) -> int:
    """Stop Open Library acting for patrons, and revoke what it holds."""
    existing = _openlibrary_client()
    if existing is None:
        print("Open Library is not connected.")
        return 0
    if existing.disabled_at:
        print(f"Open Library was already disconnected at {existing.disabled_at}.")
        return 0
    revoked = OAuthClient.disable(existing.client_id)
    print(f"Disconnected Open Library, revoking {revoked} live token(s).")
    print("  Patrons keep their loans; Open Library can no longer read or make them.")
    return 0


def cmd_register(args) -> int:
    """Register a consumer. The secret is printed once and never recoverable."""
    scopes = args.scope or sorted(SCOPES)
    if unknown := set(scopes) - set(SCOPES):
        print(f"Unknown scope(s): {' '.join(sorted(unknown))}. "
              f"Available: {' '.join(sorted(SCOPES))}.", file=sys.stderr)
        return 1

    try:
        client, secret = OAuthClient.register(
            name=args.name, redirect_uris=list(args.redirect_uri), scopes=scopes,
            is_confidential=not args.public,
        )
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Registered {client.name!r}")
    print(f"  client_id     {client.client_id}")
    if secret:
        print(f"  client_secret {secret}")
        print("\n  Give the secret to the consumer now — only its hash is stored,")
        print("  so this is the one time it can be read.")
    else:
        print("  client_secret (none — public client, authenticates with PKCE alone)")
    print(f"  scopes        {client.scopes}")
    print(f"  redirect_uris {' '.join(args.redirect_uri)}")
    return 0


def cmd_list(args) -> int:
    """Client ids are server-generated, so this is the only way to find one."""
    rows = db.query(OAuthClient).order_by(OAuthClient.created_at.desc()).all()
    if not rows:
        print("No registered clients.")
        return 0
    print(f"{'client_id':26} {'status':9} {'name':28} redirect_uris")
    for row in rows:
        status = "disabled" if row.disabled_at else "active"
        uris = ", ".join(row.redirect_uris.split())
        print(f"{row.client_id:26} {status:9} {row.name[:28]:28} {uris[:60]}")
    return 0


def cmd_disable(args) -> int:
    client = db.query(OAuthClient).filter(
        OAuthClient.client_id == args.client_id).first()
    if client is None:
        print(f"No client with id {args.client_id!r}.", file=sys.stderr)
        return 1
    if client.disabled_at:
        print(f"{client.name!r} was already disabled at {client.disabled_at}.")
        return 0
    revoked = OAuthClient.disable(args.client_id)
    print(f"Disabled {client.name!r} and revoked {revoked} live token(s).")
    return 0


def cmd_sweep(args) -> int:
    deleted = sweep_expired(older_than_days=args.older_than_days)
    print(f"Deleted {deleted} expired row(s).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    connect = sub.add_parser(
        "ol-connect", help="let Open Library act on patrons' behalf")
    connect.add_argument("--redirect-uri", default=OPENLIBRARY_REDIRECT,
                         help=f"Open Library's callback (default: {OPENLIBRARY_REDIRECT})")
    connect.add_argument("--rotate", action="store_true",
                         help="issue a new secret, retiring the old registration")
    connect.set_defaults(fn=cmd_ol_connect)

    disconnect = sub.add_parser(
        "ol-disconnect", help="revoke Open Library's access")
    disconnect.set_defaults(fn=cmd_ol_disconnect)

    register = sub.add_parser(
        "register", help="register a consumer and print its credentials once")
    register.add_argument("name", help='display name shown on the consent screen')
    register.add_argument("redirect_uri", nargs="+",
                          help="one or more registered callback URLs")
    register.add_argument("--scope", action="append",
                          help=f"repeatable; defaults to all ({' '.join(sorted(SCOPES))})")
    register.add_argument("--public", action="store_true",
                          help="a native app that cannot keep a secret; "
                               "authenticates with PKCE alone (RFC 8252)")
    register.set_defaults(fn=cmd_register)

    sub.add_parser("list", help="show every registered client").set_defaults(fn=cmd_list)

    disable = sub.add_parser(
        "disable", help="stop a client and revoke the tokens it holds")
    disable.add_argument("client_id")
    disable.set_defaults(fn=cmd_disable)

    sweep = sub.add_parser("sweep", help="delete codes and tokens that can no longer be used")
    sweep.add_argument("--older-than-days", type=int, default=1,
                       help="grace period; keeps recently-expired codes so reuse "
                            "detection can still recognise a replay (default: 1)")
    sweep.set_defaults(fn=cmd_sweep)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
