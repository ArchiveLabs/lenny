"""
CLI for the Standard Ebooks importer. The importer itself lives in
lenny/core/standardebooks.py so the admin import endpoint can drive it too.

Books already in the library are skipped, so `-n 50` means "50 books I don't
already have" and can be run repeatedly to pull the catalog down in batches.
"""

import argparse
import sys

from lenny.core.standardebooks import StandardEbooks, import_standardebooks  # noqa: F401 (re-exported for callers)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preload StandardEbooks from Open Library")
    parser.add_argument("-n", type=int, help="Number of books to preload", default=None)
    parser.add_argument("-o", type=int, help="Offset", default=0)
    args = parser.parse_args()
    stats = import_standardebooks(limit=args.n, offset=args.o)
    if stats["ol_error"]:
        sys.exit(1)
