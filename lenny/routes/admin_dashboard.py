"""GET /admin/dashboard: one request for the admin home page.

Each part is computed on its own; a part that fails is null and the response
is still 200, so one service being down doesn't blank the page.
"""
import datetime
import logging

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import func, text

from lenny import __version__, configs
from lenny.core import imports
from lenny.core.db import run_blocking, session as db
from lenny.core.models import Item, Loan
from lenny.routes.api import _require_admin

logger = logging.getLogger(__name__)
router = APIRouter()


def _books():
    counts = dict(((enc, sub), n) for enc, sub, n in
                  db.query(Item.encrypted, Item.subscription, func.count(Item.id))
                  .group_by(Item.encrypted, Item.subscription))
    open_, borrowable = counts.get((False, False), 0), counts.get((True, False), 0)
    subscription = sum(n for (enc, sub), n in counts.items() if sub)
    return {"total": sum(counts.values()), "open": open_, "borrowable": borrowable, "subscription": subscription}


def _loans():
    now = datetime.datetime.now(datetime.timezone.utc)
    active = db.query(func.count(Loan.id)).filter(*Loan._active_filters()).scalar()
    overdue = db.query(func.count(Loan.id)).filter(Loan.returned_at == None, Loan.due_date <= now).scalar()  # noqa: E711
    started = db.query(func.count(Loan.id)).filter(Loan.created_at >= now - datetime.timedelta(days=30)).scalar()
    return {"active": active, "overdue": overdue, "started_30d": started}


def _imports():
    statuses = [r.get("status") for r in imports.ImportJob.list()]
    return {"pending": sum(s in (imports.PENDING, imports.DOWNLOADING) for s in statuses),
            "failed": statuses.count(imports.FAILED)}


def _ok(check) -> str:
    try:
        check()
        return "ok"
    except Exception:
        db.rollback()
        return "down"


def _health():
    from lenny.core import s3

    def reader():
        r = httpx.get(f"http://lenny_reader:{configs.READER_PORT}/", timeout=2.0)
        if r.status_code >= 500:
            raise RuntimeError(r.status_code)

    return {
        "database": _ok(lambda: db.execute(text("SELECT 1"))),
        "storage": _ok(lambda: s3.head_bucket(Bucket=s3.BOOKSHELF_BUCKET)),
        "reader": _ok(reader),
        "version": __version__,
    }


def _part(name, fn):
    try:
        return fn()
    except Exception:
        db.rollback()
        logger.exception("Dashboard part %s failed", name)
        return None


def _dashboard():
    return {name: _part(name, fn) for name, fn in
            (("books", _books), ("loans", _loans), ("imports", _imports), ("health", _health))}


@router.get("/admin/dashboard")
async def dashboard(request: Request):
    _require_admin(request)
    return await run_blocking(_dashboard)
