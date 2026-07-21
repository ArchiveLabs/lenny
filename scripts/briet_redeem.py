"""
CLI for redeeming a BRIET bundle code.

Redeems the code against BRIET, then downloads and imports every book it
returns. Must run inside the lenny_api container (LennyClient uploads to
localhost:1337). Progress is also recorded to /v1/api/admin/imports.

    make briet-redeem code=ABC123
"""

import argparse
import logging
import sys

import httpx

from lenny.core.briet import import_briet

logging.basicConfig(level=logging.INFO, format="%(message)s")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Redeem a BRIET code and import its books")
    parser.add_argument("code", help="The BRIET redeem code")
    parser.add_argument(
        "--open-access",
        action="store_true",
        help="Import as open access instead of the default lendable (login-required)",
    )
    args = parser.parse_args()

    try:
        stats = import_briet(args.code, encrypted=not args.open_access)
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if 400 <= status < 500 and status != 429:
            print(f"[✗] Code rejected by BRIET ({status}) — invalid or already redeemed")
        else:
            print(f"[✗] BRIET is unavailable ({status}) — try again shortly")
        sys.exit(1)
    except httpx.HTTPError as e:
        print(f"[✗] Could not reach BRIET: {e}")
        sys.exit(1)

    print(
        f"[✓] Redeemed {stats['redeemed']} book(s) — "
        f"imported: {stats['uploaded']}, failed: {stats['failed']}"
    )
    sys.exit(1 if stats["failed"] else 0)
