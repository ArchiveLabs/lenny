"""Admin-facing loan listing logic.

Lives in core because route handlers are HTTP plumbing only — enrichment,
status derivation, and OL title resolution belong here so they can be unit
tested and reused.

Patron identity disclosure
--------------------------
`Loan.patron_email_hash` is a one-way HMAC-SHA256 of the patron's email,
keyed from the per-install `LENNY_SEED` (see
`lenny.core.utils.hash_email`). It is intentionally not reversible, no
plaintext is stored, and a list of addresses cannot be matched against it
without the key. Admin UI shows the first 12 hex chars of the
hash as a stable opaque identifier.
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from sqlalchemy import or_

from lenny.core.db import session as db
from lenny.core.models import Item, Loan
from lenny.core.openlibrary import OpenLibrary

logger = logging.getLogger(__name__)

# Cap returned rows to protect the OL enrichment call and the DB query.
_MAX_LOANS_RETURNED = 5000
_DEFAULT_LIMIT = 500

VALID_STATUSES = ("all", "active", "returned", "overdue")
# Public sort key → ORM column. Route validates against the keys.
_SORT_COLUMNS = {
    "borrowed_at": Loan.created_at,
    "due_at": Loan.due_date,
    "returned_at": Loan.returned_at,
}
VALID_SORTS = tuple(_SORT_COLUMNS)


def _loan_status(loan: Loan, now: datetime.datetime) -> str:
    """Derive display status without mutating the row.

    *now* is tz-aware (UTC). Postgres returns tz-aware datetimes, but SQLite
    (tests) drops the tz and returns naive values — so coerce a naive due_date
    to UTC before comparing, otherwise the comparison raises TypeError.
    """
    if loan.returned_at is not None:
        return "returned"
    due = loan.due_date
    if due is not None:
        if due.tzinfo is None:
            due = due.replace(tzinfo=datetime.timezone.utc)
        if due < now:
            return "overdue"
    return "active"


# Edition titles are effectively immutable — cache them for the life of the
# worker process so repeat loan pages (same editions, over and over) don't
# re-hit openlibrary.org on every request. That live call was the source of
# multi-second/timeout latency on GET /admin/loans under load.
_TITLE_CACHE: dict[int, str] = {}


def _resolve_titles(edition_ids: list[int]) -> dict[int, str]:
    """Batch-fetch OL titles for a set of edition integers.

    Returns {edition_int: title}. Missing or failed lookups are simply
    absent from the map — callers should treat a miss as "title unknown"
    and fall back to the edition key.
    """
    if not edition_ids:
        return {}

    uncached = [eid for eid in edition_ids if eid not in _TITLE_CACHE]
    if uncached:
        olid_query = " OR ".join(f"OL{eid}M" for eid in uncached)
        query = f"edition_key:({olid_query})"
        try:
            records = OpenLibrary.search(query=query, fields=["title", "edition_key"])
            for rec in records:
                try:
                    _TITLE_CACHE[int(rec.olid)] = getattr(rec, "title", "") or ""
                except (AttributeError, TypeError, ValueError):
                    continue
        except Exception as exc:
            logger.warning("OL title resolution failed for %d ids: %s", len(uncached), exc)

    return {eid: _TITLE_CACHE[eid] for eid in edition_ids if eid in _TITLE_CACHE}


def _user_identifier(loan: Loan) -> str:
    """Opaque 12-char prefix of the SHA-256 patron hash. Hash is one-way."""
    return (loan.patron_email_hash or "")[:12]


def _status_filters(status: str, now: datetime.datetime) -> list:
    """SQL clauses for a status filter. Empty list == no filter ("all").

    Boundaries mirror Loan._active_filters(): a loan exactly at its due moment
    is still active (due_date > now), so overdue is due_date <= now. The
    microsecond boundary vs. _loan_status()'s display string is irrelevant.
    """
    if status == "returned":
        return [Loan.returned_at.isnot(None)]
    if status == "active":
        return [Loan.returned_at.is_(None), or_(Loan.due_date.is_(None), Loan.due_date > now)]
    if status == "overdue":
        return [Loan.returned_at.is_(None), Loan.due_date.isnot(None), Loan.due_date <= now]
    return []  # "all"


def _shape_rows(rows: list, now: datetime.datetime) -> list[dict]:
    """Enrich (Loan, Item) rows into the `AdminLoan` dict shape.

    OL titles are resolved for the page's editions only (one batch call), so
    this stays cheap regardless of the total loan count."""
    unique_editions = list({
        item.openlibrary_edition
        for _loan, item in rows
        if item.openlibrary_edition is not None
    })
    title_map = _resolve_titles(unique_editions)

    out: list[dict] = []
    for loan, item in rows:
        edition_int = item.openlibrary_edition
        edition_key = f"OL{edition_int}M" if edition_int else ""
        out.append({
            "id": loan.id,
            "user_identifier": _user_identifier(loan),
            "book_title": title_map.get(edition_int, ""),
            "edition_key": edition_key,
            "borrowed_at": loan.created_at.isoformat() if loan.created_at else None,
            "due_at": loan.due_date.isoformat() if loan.due_date else None,
            "returned_at": loan.returned_at.isoformat() if loan.returned_at else None,
            "status": _loan_status(loan, now),
        })
    return out


def query_loans_for_admin(
    *,
    status: str = "all",
    user: Optional[str] = None,
    edition: Optional[int] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    sort: str = "borrowed_at",
    order: str = "desc",
) -> tuple[list[dict], int]:
    """Filtered, paginated loan listing for the admin UI.

    Returns ``(items, total)`` where *total* is the count matching the filters
    (before limit/offset) so the UI can render "showing X of N".

    Args:
      status: one of ``VALID_STATUSES``.
      user:   hex prefix of a patron hash (already validated by the caller);
              matched with ``patron_email_hash LIKE '<prefix>%'``. Restricted to
              hex chars upstream, so no LIKE-wildcard injection is possible.
      limit/offset: page window (limit capped at ``_MAX_LOANS_RETURNED``).
      sort:   one of ``VALID_SORTS``; order: ``asc`` | ``desc``.

    Raises ValueError on an invalid status/sort/order (the route maps to 400).
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    if sort not in _SORT_COLUMNS:
        raise ValueError(f"invalid sort: {sort!r}")
    if order not in ("asc", "desc"):
        raise ValueError(f"invalid order: {order!r}")

    cap = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LOANS_RETURNED))
    off = max(0, int(offset or 0))
    now = datetime.datetime.now(datetime.timezone.utc)

    filters = _status_filters(status, now)
    if user:
        filters.append(Loan.patron_email_hash.like(f"{user}%"))
    if edition is not None:
        filters.append(Item.openlibrary_edition == edition)

    base = db.query(Loan, Item).join(Item, Loan.item_id == Item.id).filter(*filters)
    total = base.count()

    col = _SORT_COLUMNS[sort]
    primary = col.desc() if order == "desc" else col.asc()
    # Stable tiebreak by id so pagination is deterministic when sort keys tie.
    rows = base.order_by(primary, Loan.id.desc()).offset(off).limit(cap).all()

    return _shape_rows(rows, now), total


def list_loans_for_admin(limit: Optional[int] = None) -> list[dict]:
    """Back-compat flat listing (newest first), no filtering or total.

    Kept so existing callers of the bare ``GET /admin/loans`` response keep
    working. New callers should use :func:`query_loans_for_admin`.
    """
    items, _total = query_loans_for_admin(limit=limit)
    return items
