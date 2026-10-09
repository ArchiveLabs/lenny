from pathlib import Path
from typing import Optional
from fastapi import UploadFile, Request
from botocore.exceptions import ClientError
import os as _os
import time as _time
import requests as _requests
import httpx as _httpx
import logging

logger = logging.getLogger(__name__)
from pyopds2_lenny import LennyDataProvider, LennyDataRecord, build_post_borrow_publication
from pyopds2 import Catalog, Metadata
from pyopds2.models import Link, Navigation
from pyopds2.provider import DataProvider
from lenny import configs as _configs
from lenny.core import db, s3, auth
from lenny.core.utils import hash_email, parse_modified_since, to_iso_utc
from lenny.core.models import Item, FormatEnum, Loan
from lenny.core.openlibrary import OpenLibrary
from lenny.core.upstream import open_library as _ol_guard, install_timeout as _install_ol_timeout
import pyopds2_openlibrary as _pyopds2_openlibrary
from lenny.core.exceptions import (
    ItemExistsError,
    InvalidFileError,
    DatabaseInsertError,
    DatabaseDeleteError,
    DatabaseUpdateError,
    FileTooLargeError,
    S3UploadError,
    EmailNotFoundError,
    ItemNotFoundError,
    LoanNotFoundError
)

from lenny.configs import (
    SCHEME, HOST, PORT, PROXY,
    READER_PORT, LOAN_LIMIT, AUTH_MODE_DIRECT
)
from urllib.parse import quote, urlencode

def _make_url(path):
    if PROXY:
        return f"{PROXY}{path}"
    url = f"{SCHEME}://{HOST}"
    if PORT and PORT not in {80, 443}:
        url += f":{PORT}"
    return f"{url}{path}"

_install_ol_timeout(_pyopds2_openlibrary)


def _guarded_search(**kw):
    """LennyDataProvider.search with a deadline, a breaker and a stale fallback."""
    key = (kw.get("query"), kw.get("limit"), kw.get("offset"))
    return _ol_guard.call(key, LennyDataProvider.search, **kw)


LennyDataProvider.BASE_URL = _make_url("/v1/api/")
# Same configured issuer as routes.oauth2.issuer_url, never the Host header. Lets
# the library build an Authorization Code + PKCE entry; auth_document() decides
# whether a client is shown it.
LennyDataProvider.OAUTH_ISSUER = _make_url("").rstrip("/")



def auth_document() -> dict:
    """The OPDS Authentication Document for the flow the admin has made active.

    One flow at a time, following the admin switch: `external` (OIDC provider
    configured and enabled) advertises Authorization Code + PKCE only; every
    other mode keeps the implicit entry exactly as before. A node that never
    configured an external provider therefore never advertises PKCE.

    Escape hatch for a node whose readers only speak the implicit flow:
    LENNY_AUTH_DOC_MODE=both advertises the implicit entry and then the PKCE
    entry, in any mode, so an upgrade strands no existing reader.
    ponytail: "both" becomes the default once the revised spec settles (#237).
    """
    if _os.environ.get("LENNY_AUTH_DOC_MODE", "active").strip().lower() == "both":
        flows = ["implicit", "pkce"]
    else:
        flows = ["pkce"] if _configs.read_lending_mode() == "external" else ["implicit"]
    # The library picks the entries and names the document (`id`) after them.
    return LennyDataProvider.get_authentication_document(flows)

# empty_catalog / build_catalog / build_publication are not yet in the
# pyopds2_lenny library (pinned to commit 356518d). Patch them here so
# routes and tests can use/mock them without touching the library.

def _opds_url(base: str, offset=None, limit=None, modified_since=None) -> str:
    """`{base}opds`, carrying forward whichever paging/filter params are in play.

    A `next` link has to reproduce the caller's filter or the consumer silently
    walks off the filtered set and back onto the full catalogue on page two.
    """
    params = [
        (key, value)
        for key, value in (
            ("modified_since", modified_since),
            ("offset", offset),
            ("limit", limit),
        )
        if value is not None
    ]
    return f"{base}opds" + (f"?{urlencode(params)}" if params else "")


def _lenny_catalog_links(base: str, page: Optional[dict] = None) -> list:
    """Catalog-level links. `page` (offset/limit/modified_since/total), when given,
    makes `self` reflect the request that was actually made and adds `next` while
    more items remain — OPDS 2.0 pagination, and the only way a harvester following
    `rel=next` can see past the first page."""
    page = page or {}
    offset = page.get("offset") or 0
    limit = page.get("limit")
    modified_since = page.get("modified_since")
    total = page.get("total")

    links = [
        Link(
            rel="self",
            href=_opds_url(base, offset=offset or None, limit=limit,
                           modified_since=modified_since),
            type="application/opds+json",
        ),
        Link(
            rel="search",
            href=f"{base}opds/search{{?query}}",
            type="application/opds+json",
            templated=True,
        ),
        Link(rel="http://opds-spec.org/shelf", href=f"{base}shelf", type="application/opds+json"),
        Link(rel="profile", href=f"{base}profile", type="application/opds-profile+json"),
    ]

    if limit and total is not None and (offset + limit) < total:
        links.append(
            Link(
                rel="next",
                href=_opds_url(base, offset=offset + limit, limit=limit,
                               modified_since=modified_since),
                type="application/opds+json",
            )
        )
    return links


def _modified_for(record, modified_map: Optional[dict]) -> Optional[str]:
    """The ISO 8601 `modified` stamp for a record, or None.

    Keyed off `lenny_id` — the same key `encryption_map`/`borrowable_map` already
    use — so a timestamp travels with the item it belongs to rather than with a
    position in the Open Library search response.
    """
    if not modified_map:
        return None
    lenny_id = getattr(record, "lenny_id", None)
    return modified_map.get(lenny_id) if lenny_id is not None else None


def _stamp_modified(pub_dict: dict, modified: Optional[str]) -> dict:
    """Write `metadata.modified` onto an already-serialized publication.

    It has to happen *after* serialization. `pyopds2.Metadata.modified` is typed
    `datetime`, so handing pydantic an ISO string gets it parsed back into a
    datetime object — and routes pass the result straight to `json.dumps`, which
    cannot serialize one. Stamping the dumped dict keeps the feed JSON-safe
    without depending on pydantic's dump mode.
    """
    if modified:
        pub_dict.setdefault("metadata", {})["modified"] = modified
    return pub_dict


@classmethod
def _lenny_empty_catalog(cls, limit: int = 50, auth_mode_direct: bool = False, title: str = "Lenny Catalog",
                         page: Optional[dict] = None) -> dict:
    catalog = Catalog(
        metadata=Metadata(title=title, numberOfItems=0),
        links=_lenny_catalog_links(cls.BASE_URL, page=page),
        publications=[],
    )
    return catalog.model_dump(exclude_none=True)


@classmethod
def _lenny_build_catalog(cls, search_response, auth_mode_direct: bool = False, title: str = "Lenny Catalog",
                         modified_map: Optional[dict] = None, page: Optional[dict] = None) -> dict:
    publications = []
    stamps = []
    for record in search_response.records:
        if isinstance(record, LennyDataRecord):
            record.auth_mode_direct = auth_mode_direct
        pub = record.to_publication()
        pub_dict = pub.model_dump(exclude_none=True)
        pub_dict["links"] = [lnk.model_dump(exclude_none=True) for lnk in record.links()]
        publications.append(pub_dict)
        stamps.append(_modified_for(record, modified_map))

    # `numberOfItems` is the size of the whole matching set, not of this page —
    # a harvester uses it to know how far it still has to walk.
    total = (page or {}).get("total")
    catalog = Catalog(
        metadata=Metadata(
            title=title,
            numberOfItems=len(publications) if total is None else total,
        ),
        links=_lenny_catalog_links(cls.BASE_URL, page=page),
        publications=publications,
    )
    feed = catalog.model_dump(exclude_none=True)

    # Stamp after the dump: `Catalog.publications` is typed `List[Publication]`,
    # so anything put on the dicts above is re-validated by pydantic on the way
    # in — which would turn our ISO string back into a datetime and make the feed
    # unserializable. `model_dump` preserves list order, so index alignment with
    # `stamps` holds.
    for pub_dict, modified in zip(feed.get("publications") or [], stamps):
        _stamp_modified(pub_dict, modified)
    return feed


@classmethod
def _lenny_build_publication(cls, record, auth_mode_direct: bool = False,
                             modified_map: Optional[dict] = None) -> dict:
    if isinstance(record, LennyDataRecord):
        record.auth_mode_direct = auth_mode_direct
    pub = record.to_publication()
    # Safe to stamp directly: this dict is returned as-is, never fed back through
    # a pydantic model, so the ISO string survives to json.dumps intact.
    pub_dict = pub.model_dump(exclude_none=True)
    pub_dict["links"] = [lnk.model_dump(exclude_none=True) for lnk in record.links()]
    return _stamp_modified(pub_dict, _modified_for(record, modified_map))


LennyDataProvider.empty_catalog = _lenny_empty_catalog
LennyDataProvider.build_catalog = _lenny_build_catalog
LennyDataProvider.build_publication = _lenny_build_publication


class LennyAPI:

    DEFAULT_LIMIT = 50
    OPDS_TITLE = "Lenny Catalog"
    MAX_FILE_SIZE = 100 * 1024 * 1024
    VALID_EXTS = {
        ".pdf": FormatEnum.PDF,
        ".epub": FormatEnum.EPUB
    }
    SEARCH_BATCH_SIZE = 250
    SEARCH_MAX_RESULTS = 100
    Item = Item
    
    @classmethod
    def make_manifest_url(cls, book_id):
        return cls.make_url(f"/v1/api/items/{book_id}/readium/manifest.json")
    
    @classmethod
    def encoded_manifest_url(cls, book_id):
        manifest_uri = cls.make_manifest_url(book_id)
        return quote(manifest_uri, safe='')

    @classmethod
    def make_url(cls, path):
        """Constructs a public Lenny URL that points to the public HOST and PORT
        """
        return _make_url(path)


    @classmethod
    def auth_check(cls, item, session: str=None, request: Request=None):
        """
        Checks if the user is allowed to access the book.

        For encrypted items: verifies session, then checks for an active loan.
        Does NOT auto-borrow — callers that want auto-borrow use the /borrow endpoint.
        """
        success = {"success": "authenticated"}
        ip = request.client.host
        redir = request.url.path

        if item.is_login_required:
            email_data = auth.verify_session_cookie(session, ip)
            if not email_data:
                return {
                    "error": "unauthenticated",
                    "url": f"/v1/api/authenticate?redir={redir}",
                    "required": ["email"],
                    "message": "Not authenticated; POST to url to get a one-time-password"
                }
            email = email_data.get("email") if isinstance(email_data, dict) else email_data
            success['email'] = email
            if not Loan.exists(item.id, email):
                return {
                    "error": "unauthorized",
                    "url": f"/v1/api/items/{item.openlibrary_edition}/borrow",
                    "message": "Book must be borrowed before being read"
                }
        return success
    
    @classmethod
    def make_session_cookie(cls, email: str):
        """Compatibility wrapper: create a session cookie using auth helpers."""
        return auth.create_session_cookie(email)

    @classmethod
    def validate_session_cookie(cls, session_cookie: str):
        """Validates the session cookie and returns the email if valid."""
        if session_cookie:
            email_data = auth.verify_session_cookie(session_cookie)
            return email_data.get("email") if isinstance(email_data, dict) else email_data
        return None

    # GET /admin/items enriches every row with a live OL search on every
    # request — measured at ~9s for 16 items (the DB query itself is ~15ms),
    # which is the same class of latency /admin/loans had before its title/
    # author columns were denormalized. That fix doesn't apply cleanly here:
    # this endpoint's contract is the full public-item shape (description,
    # cover, etc.), not just title/author, so there's more to cache than two
    # columns. A short TTL cache on the OL-only portion, keyed by the exact
    # edition set, cuts repeat requests (the common case: reloading the same
    # admin listing page) to ~0 while leaving Lenny-owned fields (encrypted,
    # loan_duration_days, availability) always fresh — those come from
    # `imap`, re-merged on every call regardless of cache hit/miss.
    _ENRICH_CACHE_TTL_S = 300
    _enrich_cache: dict = {}

    @classmethod
    def _enrich_items(cls, items, fields=None, limit=None):
        imap = dict((i.openlibrary_edition, i) for i in items)
        if not imap:
            return {}
        edition_ids = tuple(sorted(imap.keys()))
        cache_key = (edition_ids, tuple(sorted(fields)) if fields else None)

        now = _time.monotonic()
        cached = cls._enrich_cache.get(cache_key)
        if cached and cached[0] > now:
            ol_by_olid = cached[1]
        else:
            q = f"edition_key:({' OR '.join(f'OL{eid}M' for eid in edition_ids)})"
            ol_by_olid = {int(book.olid): book for book in OpenLibrary.search(query=q, fields=fields)}
            # ponytail: crude cap instead of real LRU eviction — fine for a
            # cache this size/TTL, revisit if the key space grows a lot.
            if len(cls._enrich_cache) > 256:
                cls._enrich_cache.clear()
            cls._enrich_cache[cache_key] = (now + cls._ENRICH_CACHE_TTL_S, ol_by_olid)

        return {olid: book + {"lenny": imap[olid]} for olid, book in ol_by_olid.items()}

    # Deliberately no local title/author storage and no caching here (unlike
    # _enrich_items above) — an earlier version denormalized title/author
    # onto Item for this, which turned out more machinery than wanted for
    # what's a live catalog anyway. Same batched-query pattern as
    # search_feed (patron-facing /opds/search): every call re-asks Open
    # Library, scoped to editions Lenny actually holds. Plain listing
    # (no search term) is GET /admin/items' job, already cached there —
    # this only answers an actual `q`.
    @staticmethod
    def _prefix_query(q: str) -> str:
        """Appends a trailing wildcard to the last word of `q`.

        OL's search.json does whole-word/stemmed matching by default — 'the
        suit' will never match "The Suitors", the same way normal full-text
        search doesn't match a partial word. An admin typing into a search
        box is almost always mid-word, so without this, correctly-typed
        partial titles read as "search is broken" even though OL answered
        correctly and the endpoint is working exactly as built. Confirmed
        directly against OL: 'suit*' and 'the suit*' both match "The
        Suitors"; 'the suit' does not. Only the last token gets the
        wildcard — earlier words are assumed already finished.
        """
        parts = q.rsplit(None, 1)
        if not parts:
            return q
        if len(parts) == 1:
            return f"{parts[0]}*"
        return f"{parts[0]} {parts[1]}*"

    @classmethod
    def admin_search_items(
        cls, q: str, encrypted: Optional[bool] = None, limit: Optional[int] = None
    ) -> tuple[list[dict], bool]:
        """Returns `(items, ol_unavailable)`. `ol_unavailable=True` means a
        batch failed to reach Open Library — the caller can't tell that
        apart from "zero real matches" just by looking at an empty list
        otherwise, which is exactly what a transient OL outage looked like
        from the admin UI (silently empty, not an error)."""
        limit = max(1, min(int(limit or 50), 200))
        all_items = Item.get_all()
        if encrypted is not None:
            all_items = {k: v for k, v in all_items.items() if v.encrypted == encrypted}
        if not all_items:
            return [], False

        olid_list = list(all_items.keys())
        batches = [
            olid_list[i:i + cls.SEARCH_BATCH_SIZE]
            for i in range(0, len(olid_list), cls.SEARCH_BATCH_SIZE)
        ]

        prefixed_q = cls._prefix_query(q)
        collected: list[dict] = []
        seen: set[int] = set()
        try:
            for batch in batches:
                search_query = f"{prefixed_q} AND {cls._edition_key_query(batch)}"
                for book in OpenLibrary.search(
                    query=search_query, fields=["title", "author_name", "edition_key"], limit=limit
                ):
                    try:
                        eid = int(book.olid)
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if eid not in all_items or eid in seen:
                        continue
                    seen.add(eid)
                    item = all_items[eid]
                    authors = getattr(book, "author_name", None) or []
                    collected.append({
                        "id": item.id,
                        "edition_key": f"OL{eid}M",
                        "title": getattr(book, "title", None),
                        "author": ", ".join(authors) if authors else None,
                        "encrypted": item.encrypted,
                        "formats": item.formats.name if item.formats else None,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    })
                    if len(collected) >= limit:
                        break
                if len(collected) >= limit:
                    break
        except (_requests.exceptions.RequestException, _httpx.HTTPError) as e:
            logger.warning(f"Open Library unreachable during admin item search: {e}")
            return collected, True

        return collected, False

    @classmethod
    def get_enriched_items(cls, olid=None, fields=None, offset=None, limit=None, encrypted=None,
                           modified_since=None):
        """Returns a dict whose keys are int `olid` Open Library
        edition IDs and whose values are OpenLibraryRecords with an
        additional `lenny` field containing Lenny's record for this
        item in the LennyDB
        """
        limit = limit or cls.DEFAULT_LIMIT
        items = [Item.exists(olid)] if olid else Item.get_many(
            offset=offset, limit=limit, encrypted=encrypted, modified_since=modified_since
        )
        return cls._enrich_items(items, fields=fields)

    @classmethod
    def _modified_map(cls, items) -> dict:
        """{lenny edition id -> ISO 8601 UTC `updated_at`} for local `Item` rows.

        Keyed by the same id the OPDS records carry as `lenny_id`, so publications
        can be stamped without relying on the position of a record in the OL
        search response. `updated_at` is Lenny's own column, so like the
        encryption and availability flags it needs no upstream request.
        """
        modified = {}
        for item in items:
            if item is None:
                continue
            try:
                edition_id = int(item.openlibrary_edition)
            except (AttributeError, TypeError, ValueError):
                continue
            if stamp := to_iso_utc(getattr(item, "updated_at", None)):
                modified[edition_id] = stamp
        return modified

    @classmethod
    def opds_feed(cls, olid=None, offset=None, limit=None, query=None, auth_mode_direct=None, email=None,
                  email_hashed=False, modified_since=None):
        """
        Generate an OPDS 2.0 catalog using the opds2 Catalog.create helper
        and the LennyDataProvider to transform Open Library metadata into
        OPDS Publications with Lenny borrow/return links.

        `modified_since` (a datetime, or an ISO 8601 date/timestamp string) limits
        the feed to items changed at or after that instant, oldest change first.
        Together with the `rel=next` link and each publication's
        `metadata.modified`, that is what lets a consumer harvest incrementally
        instead of refetching the whole catalogue — see
        internetarchive/openlibrary#13241.

        Exactly one Open Library search is issued, by LennyDataProvider. The
        edition ids, encryption flags, availability and modification stamps all
        come from the local `Item` rows, so there is nothing to look up upstream
        first.
        """
        use_direct = auth_mode_direct if auth_mode_direct is not None else AUTH_MODE_DIRECT

        # If requesting single item and user is authenticated, check for active loan
        if olid and email:
            if item := Item.exists(olid):
                if item.is_login_required and Loan.exists(item.id, email, hashed=email_hashed):
                    return build_post_borrow_publication(olid, auth_mode_direct=use_direct)

        limit = limit or cls.DEFAULT_LIMIT
        offset = offset or 0
        modified_since = parse_modified_since(modified_since)

        # Paging context for the catalog links. A single-publication request is
        # not a paged collection, so it carries none.
        page = None if olid else {
            "offset": offset,
            "limit": limit,
            "modified_since": to_iso_utc(modified_since),
            "total": Item.count(modified_since=modified_since),
        }

        items = [Item.exists(olid)] if olid else Item.get_many(
            offset=offset, limit=limit, modified_since=modified_since
        )
        edition_ids, encryption_map, borrowable_map = cls._local_item_maps(items)
        if not edition_ids:
            return LennyDataProvider.empty_catalog(limit=limit, auth_mode_direct=use_direct, page=page)

        modified_map = cls._modified_map(items)

        try:
            search_response = _guarded_search(
                query=cls._edition_key_query(edition_ids),
                limit=limit,
                # Paging already happened in the DB query above, and `query`
                # names only this page's editions — so this must ask for page 1.
                # Forwarding `offset` asked Open Library for page
                # `offset // limit + 1` of a result set that never has more than
                # one page, which is why /opds?offset=50 served an empty feed,
                # and why `rel=next` needs this fixed to be worth emitting.
                offset=0,
                # Keyed `{edition_id: edition_id}`: LennyDataProvider assigns
                # `lenny_id` from each record's own OL edition key and uses this
                # only as a membership set (ArchiveLabs/pyopds2_lenny#31). OL
                # neither preserves the order of the `edition_key:(...)`
                # disjunction nor returns a record for every id, so nothing
                # positional survives contact with it.
                lenny_ids={edition_id: edition_id for edition_id in edition_ids},
                encryption_map=encryption_map,
                borrowable_map=borrowable_map,
            )
        except (_requests.exceptions.RequestException, _httpx.HTTPError) as e:
            logger.warning(f"Open Library unreachable during OPDS feed build: {e}")
            return LennyDataProvider.empty_catalog(limit=limit, auth_mode_direct=use_direct, page=page)

        # Open Library knows none of these editions. Previously this was caught
        # a step earlier, by the enrichment search returning no rows. Still a
        # page of a paged collection, so it keeps its paging context — a
        # harvester walking `rel=next` must not lose its place because one
        # page happened to come back empty.
        if not search_response.records:
            return LennyDataProvider.empty_catalog(
                limit=limit, auth_mode_direct=use_direct, page=page
            )

        for record in search_response.records:
            if isinstance(record, LennyDataRecord):
                record.auth_mode_direct = use_direct

        if olid:
            return LennyDataProvider.build_publication(
                search_response.records[0], auth_mode_direct=use_direct, modified_map=modified_map
            )

        return LennyDataProvider.build_catalog(
            search_response, auth_mode_direct=use_direct,
            modified_map=modified_map, page=page,
        )

    @classmethod
    def _local_item_maps(cls, items):
        """Derive an OPDS feed's edition ids and per-item flags from local rows.

        `lenny_id` is the Open Library edition number, and encryption and
        availability are Lenny's own state -- none of it comes from Open
        Library, so none of it needs an upstream request to obtain.

        Returns `(edition_ids, encryption_map, borrowable_map)`.
        """
        edition_ids: list[int] = []
        encryption_map: dict[int, bool] = {}
        borrowable_map: dict[int, bool] = {}

        for item in items:
            if item is None:
                continue
            try:
                edition_id = int(item.openlibrary_edition)
            except (AttributeError, TypeError, ValueError):
                continue
            if edition_id in encryption_map:
                continue
            edition_ids.append(edition_id)
            encryption_map[edition_id] = item.encrypted
            borrowable_map[edition_id] = item.is_borrowable

        return edition_ids, encryption_map, borrowable_map

    @staticmethod
    def _edition_key_query(edition_ids) -> str:
        """Build the Open Library `edition_key:(OL1M OR OL2M ...)` disjunction."""
        return f"edition_key:({' OR '.join(f'OL{i}M' for i in edition_ids)})"

    @classmethod
    def search_feed(cls, query=None, limit=None, auth_mode_direct=None):
        """
        Search Lenny's catalog via OpenLibrary, constrained to local edition IDs.

        Chunks all local edition IDs into batches, queries OL with
        '{query} AND edition_key:(OL1M OR OL2M OR ...)' per batch,
        and stops once enough results are collected.

        Each batch is a single Open Library search, issued through
        LennyDataProvider so the records it returns are the ones the feed is
        built from. Nothing is fetched twice.
        """
        use_direct = auth_mode_direct if auth_mode_direct is not None else AUTH_MODE_DIRECT
        limit = min(limit or cls.DEFAULT_LIMIT, cls.SEARCH_MAX_RESULTS)

        if not query or not query.strip():
            return LennyDataProvider.empty_catalog(
                title="Search results", auth_mode_direct=use_direct
            )

        query = query.strip()
        all_items = Item.get_all()
        if not all_items:
            return LennyDataProvider.empty_catalog(
                title=f"Search results for: {query}", auth_mode_direct=use_direct
            )

        olid_list = list(all_items.keys())
        batches = [
            olid_list[i:i + cls.SEARCH_BATCH_SIZE]
            for i in range(0, len(olid_list), cls.SEARCH_BATCH_SIZE)
        ]

        collected = []
        seen: set[int] = set()
        try:
            for batch in batches:
                batch_ids, encryption_map, borrowable_map = cls._local_item_maps(
                    all_items[olid] for olid in batch
                )
                if not batch_ids:
                    continue

                response = _guarded_search(
                    query=f"{query} AND {cls._edition_key_query(batch_ids)}",
                    limit=limit,
                    lenny_ids={edition_id: edition_id for edition_id in batch_ids},
                    encryption_map=encryption_map,
                    borrowable_map=borrowable_map,
                )

                for record in response.records:
                    # No lenny_id means Open Library surfaced an edition this
                    # batch did not ask about; it is not one of Lenny's.
                    lenny_id = getattr(record, "lenny_id", None)
                    if lenny_id is None or lenny_id in seen:
                        continue
                    seen.add(lenny_id)
                    collected.append(record)
                    if len(collected) >= limit:
                        break

                if len(collected) >= limit:
                    break
        except (_requests.exceptions.RequestException, _httpx.HTTPError) as e:
            logger.warning(f"Open Library unreachable during search: {e}")
            return LennyDataProvider.empty_catalog(
                title=f"Search results for: {query}", auth_mode_direct=use_direct
            )

        if not collected:
            return LennyDataProvider.empty_catalog(
                title=f"Search results for: {query}", auth_mode_direct=use_direct
            )

        search_response = DataProvider.SearchResponse(
            provider=LennyDataProvider,
            records=collected,
            total=len(collected),
            query=query,
            limit=limit,
            offset=0,
            sort=None,
        )

        for record in search_response.records:
            if isinstance(record, LennyDataRecord):
                record.auth_mode_direct = use_direct

        return LennyDataProvider.build_catalog(
            search_response,
            title=f"Search results for: {query}",
            auth_mode_direct=use_direct,
        )

    @classmethod
    def encrypt_file(cls, f, method="lcp"):
        # XXX Not Implemented
        return f

    @classmethod
    def upload_file(cls, fp, filename):
        if not fp.size or fp.size > cls.MAX_FILE_SIZE:
            one_mb = (1024 * 1024)
            raise FileTooLargeError(
                f"{fp.filename} exceeds {cls.MAX_FILE_SIZE // one_mb}MB."
            )
        fp.file.seek(0)

        try:
            return s3.upload_fileobj(
                fp.file,
                s3.BOOKSHELF_BUCKET,
                filename,
                ExtraArgs={'ContentType': fp.content_type}
            )
        except ClientError as e:
            raise S3UploadError(
                f"Failed to upload '{fp.filename}' to S3: "
                f"{e.response.get('Error', {}).get('Message', str(e))}."
            )
        except ValueError as e:
            raise S3UploadError(
                f"File '{fp.filename}' is closed or unreadable: {e}"
            )
    
    @classmethod
    def upload_files(cls, files: list[UploadFile], filename, encrypt=False):
        from io import BytesIO
        formats = 0
        for fp in files:
            if not fp.filename:
                continue

            ext = Path(fp.filename).suffix.lower()

            if ext in cls.VALID_EXTS:
                formats += cls.VALID_EXTS[ext].value
                
                if encrypt:
                    fp.file.seek(0)
                    file_content = fp.file.read()
                    
                    fp.file.seek(0)
                    cls.upload_file(fp, f"{filename}{ext}")
                    
                    encrypted_fp = BytesIO(file_content)
                    class TempFile:
                        def __init__(self, file, filename, content_type, size):
                            self.file = file
                            self.filename = filename
                            self.content_type = content_type
                            self.size = size
                    
                    temp_file = TempFile(
                        cls.encrypt_file(encrypted_fp),
                        fp.filename,
                        fp.content_type,
                        fp.size
                    )
                    cls.upload_file(temp_file, f"{filename}_encrypted{ext}")
                else:
                    cls.upload_file(fp, f"{filename}{ext}")
            else:
                raise InvalidFileError(f"Invalid format {ext} for {fp.filename}")
        if not formats:
            raise InvalidFileError("No valid files provided")
        return formats

    @classmethod
    def add(cls, openlibrary_edition: int, files: list[UploadFile], encrypt: bool=False):
        """Adds a book into s3 and the database.

        Authorization is the caller's job and is NOT done here: every route
        reaching this must already have proved the caller is an admin
        (`_require_admin`). This used to take an `uploader_ip` and authorize on
        it, which authorized anyone who could reach the port.
        """
        if Item.exists(openlibrary_edition):
            raise ItemExistsError(f"Item '{openlibrary_edition}' already exists.")

        if formats:= cls.upload_files(files, openlibrary_edition, encrypt=encrypt):
            try:
                item = Item(
                    openlibrary_edition=openlibrary_edition,
                    encrypted=encrypt,
                    formats=FormatEnum(formats),
                )
                db.add(item)
                db.commit()
                return item
            except Exception as e:
                db.rollback()
                raise DatabaseInsertError(f"Failed to add item to db: {str(e)}.")

    UNSET = object()  # sentinel: "field not provided", distinct from JSON null

    @classmethod
    def update_item(cls, openlibrary_edition: int, encrypted=UNSET, loan_duration_days=UNSET) -> Item:
        """Update an item's encrypted/DRM flag and/or per-item loan duration.

        Callers must pass `cls.UNSET` (the default) for a field that wasn't
        part of the request — plain `None` for `loan_duration_days` means
        "clear the override, fall back to the global setting" (Loan.create
        treats a NULL column the same way), so it can't double as "unchanged".

        `encrypted`: no S3 file swap needed — `encode_book_path` (readium.py)
        always serves the plain `{id}.epub` key regardless of this flag; the
        `_encrypted` twin written at upload time is never read by anything.
        This flag is the entire access-control mechanism (gates
        `is_readable`/`is_lendable`, checked by `@requires_item_auth()` on the
        read routes), not a different file.
        """
        item = Item.exists(openlibrary_edition)
        if not item:
            raise ItemNotFoundError(f"Item '{openlibrary_edition}' not found.")

        try:
            if encrypted is not cls.UNSET:
                item.encrypted = encrypted
            if loan_duration_days is not cls.UNSET:
                item.loan_duration_days = loan_duration_days
            db.commit()
            return item
        except Exception as e:
            db.rollback()
            raise DatabaseUpdateError(f"Failed to update item {openlibrary_edition}: {str(e)}.")

    @classmethod
    def _item_s3_keys(cls, openlibrary_edition: int) -> list:
        """S3 keys belonging to exactly this item: `{olid}{ext}` and
        `{olid}_encrypted{ext}`, never a different OLID.

        `s3.get_keys(prefix=str(olid))` alone is not safe: OLID 5100863 is a
        string-prefix of 51008637, so deleting the shorter one would also
        delete the longer one's files. The character right after the numeric
        OLID is always '.' or '_' for a real match — never another digit —
        so that's the boundary check.
        """
        olid_str = str(openlibrary_edition)
        return [
            key for key in s3.get_keys(prefix=olid_str)
            if key == olid_str or key[len(olid_str):len(olid_str) + 1] in (".", "_")
        ]

    @classmethod
    def rename_item(cls, old_olid: int, new_olid: int) -> Item:
        """Repoint an item at a different OpenLibrary edition (e.g. the
        wrong edition got matched on import), moving its S3 files to match —
        openlibrary_edition is baked into the object key names, so a bare
        DB update would leave every file 404ing under the old key.

        S3 has no atomic rename: copies to the new keys first, commits the DB
        change, and only then deletes the old keys. A mid-failure leaves (at
        worst) duplicate storage under both OLIDs, never a dangling item
        pointing at a key that doesn't exist — copy failures roll back their
        own partial copies, DB failures roll back the copies too.
        """
        if old_olid == new_olid:
            item = Item.exists(old_olid)
            if not item:
                raise ItemNotFoundError(f"Item '{old_olid}' not found.")
            return item

        item = Item.exists(old_olid)
        if not item:
            raise ItemNotFoundError(f"Item '{old_olid}' not found.")
        if Item.exists(new_olid):
            raise ItemExistsError(f"Item '{new_olid}' already exists.")

        old_keys = cls._item_s3_keys(old_olid)
        old_prefix, new_prefix = str(old_olid), str(new_olid)
        copied = []
        try:
            for key in old_keys:
                new_key = new_prefix + key[len(old_prefix):]
                s3.copy_object(
                    Bucket=s3.BOOKSHELF_BUCKET,
                    CopySource={'Bucket': s3.BOOKSHELF_BUCKET, 'Key': key},
                    Key=new_key,
                )
                copied.append(new_key)
        except ClientError as e:
            for key in copied:
                try:
                    s3.delete_object(Bucket=s3.BOOKSHELF_BUCKET, Key=key)
                except ClientError:
                    pass
            raise S3UploadError(f"Failed to copy S3 objects to new OLID {new_olid}: {e}")

        try:
            item.openlibrary_edition = new_olid
            db.commit()
        except Exception as e:
            db.rollback()
            for key in copied:
                try:
                    s3.delete_object(Bucket=s3.BOOKSHELF_BUCKET, Key=key)
                except ClientError:
                    pass
            raise DatabaseUpdateError(f"Failed to rename item {old_olid} -> {new_olid}: {str(e)}.")

        for key in old_keys:
            try:
                s3.delete_object(Bucket=s3.BOOKSHELF_BUCKET, Key=key)
            except ClientError as e:
                logger.warning(f"Could not delete old S3 object '{key}' after rename: {e}")

        return item

    @classmethod
    def reupload(cls, openlibrary_edition: int, files: list, encrypt: bool = False) -> Item:
        """Replace an existing item's file(s) in place.

        Opposite guard from `add`: requires the item to already exist rather
        than rejecting if it does. Item.id and every Loan row are untouched —
        only the S3 objects and the encrypted/formats flags change, so
        history/loans survive a wrong-file correction.

        ponytail: deletes the old file(s) before uploading the new one(s)
        rather than a zero-downtime swap — correct and simple beats a brief
        (millisecond-scale) availability gap on a rare, human-triggered admin
        action. Upgrade to copy-then-delete (like rename_item) if this ever
        needs to be online-safe.
        """
        item = Item.exists(openlibrary_edition)
        if not item:
            raise ItemNotFoundError(f"Item '{openlibrary_edition}' not found.")

        for key in cls._item_s3_keys(openlibrary_edition):
            try:
                s3.delete_object(Bucket=s3.BOOKSHELF_BUCKET, Key=key)
            except ClientError as e:
                logger.warning(f"Could not delete old S3 object '{key}' before reupload: {e}")

        formats = cls.upload_files(files, openlibrary_edition, encrypt=encrypt)
        try:
            item.encrypted = encrypt
            item.formats = FormatEnum(formats)
            db.commit()
            return item
        except Exception as e:
            db.rollback()
            raise DatabaseUpdateError(f"Failed to update item {openlibrary_edition} after reupload: {str(e)}.")

    @classmethod
    def delete(cls, openlibrary_edition: int) -> None:
        """Remove an item from S3 and the database (cascades to loans).

        DB delete commits first, S3 cleanup is best-effort after. The other
        order risks a DB row (and its loans) surviving with its files
        already gone — an item that looks fine in every listing but 404s
        the moment anyone tries to read it. An orphaned S3 object if the DB
        commit fails is just wasted storage, easy to find and clean up later.
        """
        item = Item.exists(openlibrary_edition)
        if not item:
            raise ItemNotFoundError(f"Item '{openlibrary_edition}' not found.")

        try:
            db.delete(item)
            db.commit()
        except Exception as e:
            db.rollback()
            raise DatabaseDeleteError(f"Failed to delete item from db: {str(e)}.")

        for key in cls._item_s3_keys(openlibrary_edition):
            try:
                s3.delete_object(Bucket=s3.BOOKSHELF_BUCKET, Key=key)
            except ClientError as e:
                logger.warning(f"Could not delete S3 object '{key}': {e}")

    # A bulk request is admin-typed/pasted, not machine-generated — this cap
    # exists so a malformed request (e.g. an accidental huge paste) can't tie
    # up a worker looping over an unbounded list.
    MAX_BULK_DELETE = 200

    @classmethod
    def delete_many(cls, openlibrary_editions: list) -> dict:
        """Delete multiple items. One bad ID never aborts the rest.

        Returns {"deleted": [...], "not_found": [...], "failed": {olid: error}}
        so the caller (CLI or admin UI) can report a precise per-item outcome
        instead of a single pass/fail for the whole batch.
        """
        result = {"deleted": [], "not_found": [], "failed": {}}
        for olid in openlibrary_editions[:cls.MAX_BULK_DELETE]:
            try:
                cls.delete(olid)
                result["deleted"].append(olid)
            except ItemNotFoundError:
                result["not_found"].append(olid)
            except Exception as e:
                logger.error(f"Bulk delete failed for OLID {olid}: {e}")
                result["failed"][olid] = str(e)
        return result

    @classmethod
    def get_borrowed_items(cls, email: str, hashed: bool = False):
        """
        Returns active (non-returned, non-expired) Loan objects for the patron.
        Ensures openlibrary_edition is set for each loan.

        `hashed=True` means *email* is already a `hash_email()` digest, which is
        all an OAuth2 access token carries.
        """
        email_hash = email if hashed else hash_email(email)
        loans = db.query(Loan).filter(
            Loan.patron_email_hash == email_hash,
            *Loan._active_filters(),
        ).all()
        enriched_loans = []
        for loan in loans:
            item = db.query(Item).filter(Item.id == loan.item_id).first()
            if item:
                loan.openlibrary_edition = item.openlibrary_edition
                enriched_loans.append(loan)
        return enriched_loans

    @classmethod
    def get_user_profile(cls, email: str, name: Optional[str] = None, hashed: bool = False) -> dict:
        """
        Retrieves loan stats and generates the OPDS User Profile using LennyDataProvider.

        `hashed=True` means *email* is a `hash_email()` digest (all an OAuth2
        token carries). The profile then has no name or email: Lenny stores only
        the hash, and a hash is not something to show as an address.
        """
        current_loans = cls.get_borrowed_items(email, hashed=hashed)
        loans_count = len(current_loans)

        profile = LennyDataProvider.get_user_profile(
            name=None if hashed else name,
            email=None if hashed else email,
            active_loans_count=loans_count,
            loan_limit=LOAN_LIMIT
        )
        if hashed:
            for key in ("name", "email"):
                profile["metadata"].pop(key, None)
        return profile

    @classmethod
    def get_shelf_feed(cls, email: str, auth_mode_direct: bool = False, hashed: bool = False) -> dict:
        """
        Retrieves user loans, fetches their metadata, and generates the OPDS Shelf Feed.
        """
        loans = cls.get_borrowed_items(email, hashed=hashed)
        
        if not loans:
             return LennyDataProvider.get_shelf_feed([])

        olids = [f"OL{loan.openlibrary_edition}M" for loan in loans if loan.openlibrary_edition]
        lenny_ids = {int(loan.openlibrary_edition): int(loan.openlibrary_edition) for loan in loans if loan.openlibrary_edition}
        
        if not olids:
             return LennyDataProvider.get_shelf_feed([])

        query = f"edition_key:({' OR '.join(olids)})"

        try:
            resp = _guarded_search(
                query=query,
                limit=len(olids),
                lenny_ids=lenny_ids
            )
        except (_requests.exceptions.RequestException, _httpx.HTTPError) as e:
            logger.warning(f"Open Library unreachable during shelf feed build: {e}")
            return LennyDataProvider.get_shelf_feed([])

        publications = []
        for record in resp.records:
            if isinstance(record, LennyDataRecord):
                 pub = record.to_publication().model_dump()
                 if hasattr(record, 'post_borrow_links'):
                     pub["links"] = [
                         link.model_dump(exclude_none=True) 
                         for link in record.post_borrow_links()
                     ]
                 publications.append(pub)
        
        return LennyDataProvider.get_shelf_feed(publications)

    @classmethod
    def build_oauth_fragment(cls, session_cookie: str, state: str = None) -> dict:
        """Build OAuth token fragment for redirect URL or opds:// callback."""
        fragment = {
            "access_token": session_cookie,
            "token_type": "bearer",
            "expires_in": auth.COOKIE_TTL
        }
        if state:
            fragment["state"] = state
        return fragment

    @classmethod
    async def parse_request_body(cls, request: Request) -> dict:
        """Parse request body from JSON or form data, with fallback to empty dict."""
        try:
            return await request.json()
        except:
            try:
                form = await request.form()
                return dict(form)
            except:
                return {}