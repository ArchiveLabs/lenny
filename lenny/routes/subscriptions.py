"""Members-only (subscription) access: the Ghost launch and webhook, plus admin settings and stats.

    POST /subscriptions/ghost/launch          form: token, book   (from the Ghost snippet)
    POST /subscriptions/ghost/webhook         Ghost member.edited / member.deleted
    GET  /admin/settings/ghost
    PUT  /admin/settings/ghost                {site_url, allowed_tier_ids, default_access_mode}
    POST /admin/settings/ghost/webhook-secret rotate
    GET  /admin/stats/subscriptions           ?from=YYYY-MM-DD&to=YYYY-MM-DD
    GET  /admin/stats/subscriptions/books     ?from&to&page&page_size&sort=opens|patrons|last_read
"""
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func

from lenny import configs
from lenny.core import auth, ghost, ol_bootstrap
from lenny.core.api import LennyAPI
from lenny.core.briet import parse_olid
from lenny.core.db import run_blocking, session as db
from lenny.core.models import ACCESS_MODES, Item, Subscription, SubscriptionRead
from lenny.core.utils import hash_email
from lenny.routes.api import _require_admin

logger = logging.getLogger(__name__)
router = APIRouter()

_LAUNCH_PROBLEMS = {
    "not_paid": "Your membership doesn't include books yet. Check your plan on the publisher's site.",
    "tier_not_allowed": "Your membership plan doesn't include books. Check your plan on the publisher's site.",
    "token_reused": "That link was already used. Go back and press Read again.",
    "membership_changed": "Your membership just changed. Go back and press Read again.",
    "not_found": "We couldn't find that book.",
    "not_configured": "This library isn't connected to a publisher yet.",
}


def _problem(detail: str, code: str, status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code},
                        headers={"Cache-Control": "no-store"})


def _launch_failed(request: Request, code: str, status: int = 403):
    page = request.app.templates.TemplateResponse(request, "members_only.html", {
        "request": request, "heading": "We couldn't open this book",
        "explanation": _LAUNCH_PROBLEMS.get(code, "Go back to the publisher's site and press Read again."),
        "site": ghost.site_url() or None, "code": code,
    }, status_code=status, headers={"Cache-Control": "no-store"})
    return page


@router.post("/subscriptions/ghost/launch")
async def ghost_launch(request: Request, token: str = Form(""), book: str = Form("")):
    """A signed-in Ghost member pressed Read: check Ghost's entitlement token, lease, open the reader.

    The token comes in a form POST so it never lands in a URL or an access log.
    """
    olid = parse_olid(book)
    if olid is None or not await run_blocking(Item.exists, olid):
        return _launch_failed(request, "not_found", 404)
    try:
        claims = await run_blocking(ghost.verify_entitlement, token)
    except ghost.GhostError as e:
        logger.info("Ghost launch refused: %s", e.code)
        return _launch_failed(request, e.code)

    patron = hash_email(claims["sub"])
    if await run_blocking(ghost.revoked_after_issue, [patron, claims.get("member_uuid")], claims.get("iat")):
        # A webhook ended this membership after Ghost minted the token.
        return _launch_failed(request, "membership_changed")
    await run_blocking(
        Subscription.grant, ghost.PROVIDER, patron, claims.get("member_uuid"),
        claims.get("active_tier_ids") or [], ghost.LEASE_SECONDS)

    client_ip = request.client.host if request.client else None
    resp = RedirectResponse(url=f"/v1/api/items/{olid}/read", status_code=303)
    resp.set_cookie(key="session", value=auth.create_hashed_session_cookie(patron, client_ip),
                    max_age=auth.COOKIE_TTL, httponly=True, secure=True, samesite="Lax", path="/")
    return resp


@router.post("/subscriptions/ghost/webhook")
async def ghost_webhook(request: Request):
    """Ghost member.edited / member.deleted: drop access when a member stops paying.

    Never answers 410: Ghost deletes a webhook that does.
    """
    secret = ghost.webhook_secret()
    if not secret or not ghost.site_url():
        return _problem("Ghost is not connected", "not_configured", 404)
    raw = await request.body()
    if not ghost.verify_webhook_signature(raw, request.headers.get("x-ghost-signature", ""), secret):
        return _problem("Bad or missing signature", "bad_signature", 401)
    try:
        import json
        payload = json.loads(raw)
    except ValueError:
        return _problem("Body is not JSON", "bad_payload", 400)

    change = ghost.member_change(payload)
    revoked = 0
    if change:
        member_uuid, emails = change
        hashes = [hash_email(e) for e in emails]
        revoked = await run_blocking(Subscription.revoke, ghost.PROVIDER, member_uuid, hashes)
        await run_blocking(ghost.mark_revoked, [member_uuid, *hashes])
    await run_blocking(ghost.mark_webhook)
    return {"ok": True, "revoked": revoked}


# ── admin ───────────────────────────────────────────────────────────────────

def _settings() -> dict:
    site = ghost.site_url() or None
    return {
        "site_url": site,
        "allowed_tier_ids": ghost.allowed_tiers(),
        "seen_tiers": Subscription.seen_tiers(ghost.PROVIDER),
        "default_access_mode": configs.get_default_access_mode(),
        "lease_hours": ghost.LEASE_SECONDS // 3600,
        "snippet": ghost.snippet(LennyAPI.make_url("/v1/api/subscriptions/ghost/launch"), site) if site else None,
        "webhook_url": LennyAPI.make_url("/v1/api/subscriptions/ghost/webhook"),
        "webhook_secret": ghost.webhook_secret() or None,
        "last_webhook_at": ghost.last_webhook_at(),
    }


def _settings_response() -> JSONResponse:
    return JSONResponse(_settings(), headers={"Cache-Control": "no-store"})  # carries the webhook secret


@router.get("/admin/settings/ghost")
async def get_ghost_settings(request: Request):
    _require_admin(request)
    return await run_blocking(_settings_response)


@router.put("/admin/settings/ghost")
async def put_ghost_settings(request: Request):
    """Save the Ghost site and tiers, and/or the node's default access mode.

    A new site URL is checked by fetching its JWKS, so a typo fails here and not
    at the first reader. An empty site_url disconnects.
    """
    _require_admin(request)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return _problem("Body must be a JSON object", "bad_request")

    auth_updates, loan_updates = {}, {}
    if "site_url" in body:
        raw = body["site_url"] or ""
        if not isinstance(raw, str):
            return _problem("'site_url' must be a string", "invalid_site_url")
        if raw.strip():
            try:
                site = ghost.normalize_site_url(raw)
                await run_blocking(ghost.fetch_jwks, site)
            except ghost.GhostError as e:
                messages = {
                    "invalid_site_url": "Use the site's https:// address, like https://example.com",
                    "ghost_unreachable": "Couldn't reach that site. Check the address.",
                    "ghost_no_jwks": "That site doesn't look like a Ghost site with members turned on.",
                }
                return _problem(messages.get(e.code, str(e)), e.code)
            auth_updates[ghost.URL_KEY] = site
            if not ghost.webhook_secret():
                auth_updates[ghost.SECRET_KEY] = ghost.new_secret()
        else:
            auth_updates[ghost.URL_KEY] = ""
    if "allowed_tier_ids" in body:
        tiers = body["allowed_tier_ids"]
        if not isinstance(tiers, list) or not all(isinstance(t, str) and t.strip() and "," not in t for t in tiers):
            return _problem("'allowed_tier_ids' must be a list of tier ids", "invalid_tiers")
        auth_updates[ghost.TIERS_KEY] = ",".join(t.strip() for t in tiers)
    if "default_access_mode" in body:
        if body["default_access_mode"] not in ACCESS_MODES:
            return _problem("'default_access_mode' must be open, borrowable or subscription", "invalid_access_mode")
        loan_updates["LENNY_DEFAULT_ACCESS_MODE"] = body["default_access_mode"]
    if not auth_updates and not loan_updates:
        return _problem("Nothing to change", "bad_request")

    site_changed = ghost.URL_KEY in auth_updates and auth_updates[ghost.URL_KEY] != ghost.site_url()
    try:
        if auth_updates:
            ol_bootstrap.update_env_file(configs.AUTH_ENV_PATH, auth_updates)
        if loan_updates:
            ol_bootstrap.update_env_file(configs.LOAN_ENV_PATH, loan_updates)
    except OSError:
        logger.exception("Failed to save Ghost settings")
        return _problem("Could not save the settings", "env_write_failed", 500)
    if site_changed:  # the old site's members proved nothing to the new one
        await run_blocking(Subscription.revoke_all, ghost.PROVIDER)
    return await run_blocking(_settings_response)


@router.post("/admin/settings/ghost/webhook-secret")
async def rotate_ghost_webhook_secret(request: Request):
    _require_admin(request)
    secret = ghost.new_secret()
    try:
        ol_bootstrap.update_env_file(configs.AUTH_ENV_PATH, {ghost.SECRET_KEY: secret})
    except OSError:
        logger.exception("Failed to rotate the Ghost webhook secret")
        return _problem("Could not save the secret", "env_write_failed", 500)
    return JSONResponse({"webhook_secret": secret}, headers={"Cache-Control": "no-store"})


# ── stats ───────────────────────────────────────────────────────────────────

def _range(start: Optional[str], end: Optional[str]):
    today = datetime.datetime.now(datetime.timezone.utc).date()
    try:
        to = datetime.date.fromisoformat(end) if end else today
        frm = datetime.date.fromisoformat(start) if start else to - datetime.timedelta(days=29)
    except ValueError:
        return None
    if frm > to or (to - frm).days > 366:
        return None
    return frm, to


def _stats(frm, to) -> dict:
    R = SubscriptionRead
    in_range = (R.day >= frm, R.day <= to)
    books, patrons, opens = db.query(
        func.count(func.distinct(R.item_id)), func.count(func.distinct(R.patron_email_hash)),
        func.coalesce(func.sum(R.opens), 0)).filter(*in_range).one()
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=15)
    active = db.query(func.count(func.distinct(R.patron_email_hash))).filter(R.last_seen >= since).scalar()
    by_day = db.query(R.day, func.count(func.distinct(R.item_id)), func.count(func.distinct(R.patron_email_hash)),
                      func.sum(R.opens)).filter(*in_range).group_by(R.day).order_by(R.day).all()
    devices = db.query(R.device_class, func.count(func.distinct(R.patron_email_hash)),
                       func.sum(R.opens)).filter(*in_range).group_by(R.device_class).all()
    return {
        "from": frm.isoformat(), "to": to.isoformat(),
        "totals": {"books_read": books, "patrons": patrons, "opens": int(opens), "active_now": active},
        "by_day": [{"date": d.isoformat(), "books_read": b, "patrons": p, "opens": int(o)} for d, b, p, o in by_day],
        "devices": [{"device_class": c, "patrons": p, "opens": int(o)} for c, p, o in devices],
    }


@router.get("/admin/stats/subscriptions")
async def subscription_stats(request: Request):
    _require_admin(request)
    rng = _range(request.query_params.get("from"), request.query_params.get("to"))
    if rng is None:
        return _problem("Use from/to as YYYY-MM-DD, at most a year apart", "invalid_range")
    return await run_blocking(_stats, *rng)


_SORTS = {"opens": "opens", "patrons": "patrons", "last_read": "last_read"}


def _book_stats(frm, to, page, page_size, sort) -> dict:
    R = SubscriptionRead
    patrons = func.count(func.distinct(R.patron_email_hash)).label("patrons")
    opens = func.sum(R.opens).label("opens")
    last = func.max(R.last_seen).label("last_read")
    q = (db.query(Item.openlibrary_edition, patrons, opens, last).join(Item, Item.id == R.item_id)
         .filter(R.day >= frm, R.day <= to).group_by(Item.openlibrary_edition))
    total = q.count()
    order = {"opens": opens, "patrons": patrons, "last_read": last}[sort]
    rows = q.order_by(order.desc(), Item.openlibrary_edition).offset((page - 1) * page_size).limit(page_size).all()
    titles = {}
    try:  # titles are a nicety; Open Library being down must not break the stats page
        titles = {olid: (rec.get("title") if isinstance(rec, dict) else getattr(rec, "title", None)) for olid, rec in
                  LennyAPI._enrich_items([Item.exists(r[0]) for r in rows], fields=["title", "edition_key"]).items()}
    except Exception:
        logger.warning("Open Library unreachable; subscription stats without titles")
    return {"items": [{"olid": f"OL{o}M", "title": titles.get(o), "patrons": p, "opens": int(n),
                       "last_read": l.isoformat() if l else None} for o, p, n, l in rows],
            "total": total}


@router.get("/admin/stats/subscriptions/books")
async def subscription_book_stats(request: Request, page: int = 1, page_size: int = 25, sort: str = "opens"):
    _require_admin(request)
    rng = _range(request.query_params.get("from"), request.query_params.get("to"))
    if rng is None:
        return _problem("Use from/to as YYYY-MM-DD, at most a year apart", "invalid_range")
    if sort not in _SORTS:
        return _problem("'sort' must be opens, patrons or last_read", "invalid_sort")
    page, page_size = max(1, page), max(1, min(page_size, 100))
    return await run_blocking(_book_stats, *rng, page, page_size, sort)
