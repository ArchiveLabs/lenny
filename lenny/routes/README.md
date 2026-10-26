
# Lenny API Routing Documentation

Base URL: `http://localhost:8080/v1/api`

## Endpoints


### 1. Home Page

- **GET /**
  - Renders the index HTML page.
  - **Parameters:** None


### 2. Get Items

- **GET /items**
  - Returns enriched items.
  - **Query Parameters:**
    - `fields` (optional, comma-separated string): Fields to include in response
    - `offset` (optional, int): Pagination offset
    - `limit` (optional, int): Pagination limit
  - Example: `GET /items?fields=title,author&offset=0&limit=10`


### 3. OPDS Feed

- **GET /opds**
  - Returns OPDS feed.
  - Query params:
    - `offset` (int, optional): Items to skip. Paired with `limit`, pages the feed.
    - `limit` (int, optional): Items per page (default 50).
    - `modified_since` (str, optional): ISO 8601 date or timestamp
      (`2026-08-01`, `2026-08-01T12:30:00Z`). Limits the feed to items whose
      `updated_at` is at or after that instant. A value with no timezone is read
      as UTC. Returns `400` if unparseable.
  - Items are ordered oldest-change-first (`updated_at`, then `id`), so paging is
    stable and an incremental consumer can resume where it stopped.
  - The catalog carries a `rel=next` link while more items remain, preserving
    `modified_since`; each publication carries `metadata.modified`.
  - `metadata.numberOfItems` is the size of the whole matching set, not of the
    current page.
  - Example — everything changed since August 1st, 100 at a time:
    ```
    curl "http://localhost:8080/v1/api/opds?modified_since=2026-08-01&limit=100"
    ```
  - **Query Parameters:**
    - `offset` (optional, int): Pagination offset
    - `limit` (optional, int): Pagination limit


### 4. Read Book (Redirect)

- **GET /items/{book_id}/read**
  - Redirects to Thorium Web Reader for the book. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
  - **Query Parameters:**
    - `format` (optional, str, default: "epub"): Book format


### 5. Readium Manifest

- **GET /items/{book_id}/readium/manifest.json**
  - Returns Readium manifest for the book. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
  - **Query Parameters:**
    - `format` (optional, str, default: ".epub"): Book format


### 6. Proxy Readium Requests

- **GET /items/{book_id}/readium/{readium_path}**
  - Proxies requests to Readium server. Requires authentication.
  - **Path Parameters:**
    - `book_id` (str): Book identifier
    - `readium_path` (str): Path to resource in Readium
  - **Query Parameters:**
    - `format` (optional, str, default: ".epub"): Book format


### 7. Upload Item

- **POST /upload**
  - Uploads an EPUB file for an OpenLibrary edition (EPUB only; the file must be a real EPUB).
  - **Admin only.** Requires the same credential pair as every `/admin/` route:
    the `X-Admin-Internal-Secret` shared secret (from `auth.env`) *and* an admin
    Bearer token from `POST /admin/auth`. There is no IP-based exemption — a
    caller's network position never authorizes an upload.
  - **Form Data:**
    - `openlibrary_edition` (int, required): OpenLibrary Edition ID (must be positive)
    - `encrypted` (bool, optional, default: false): Set to true if file is encrypted
    - `file` (UploadFile, required): EPUB file (max 100MB)
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/upload" \
      -H "X-Admin-Internal-Secret: $ADMIN_INTERNAL_SECRET" \
      -H "Authorization: Bearer $ADMIN_TOKEN" \
      -F "openlibrary_edition=12345678" \
      -F "encrypted=false" \
      -F "file=@book.epub"
    ```


### 8. Authenticate

- **POST /authenticate**
  - Authenticates user via email and OTP. Sets session cookie on success.
  - **Form Data:**
    - `email` (str, required): User email
    - `otp` (str, required): One-time password


### 9. Borrow Item

- **POST /items/{book_id}/borrow**
  - Borrows a book for the authenticated user. Requires session cookie for encrypted books.
  - **Path Parameters:**
    - `book_id` (int): Book identifier
  - **Body Parameters:**
    - `otp` (str, optional): One-time password (if not logged in)
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/items/12345678/borrow" \
      -H "Content-Type: application/json" \
      -d '{"otp": "123456"}'
    ```


### 10. Checkout Multiple Items

- **POST /items/checkout**
  - Checks out multiple books for a user.
  - **Body Parameters (JSON):**
    - `openlibrary_editions` (List[int], required): List of OpenLibrary Edition IDs
    - `email` (str, required): User email
  - Example:
    ```sh
    curl -X POST "http://localhost:8080/v1/api/items/checkout" \
      -H "Content-Type: application/json" \
      -d '{"openlibrary_editions": [12345678, 23456789], "email": "user@example.com"}'
    ```


### 11. Return Item

- **POST /items/{book_id}/return**
  - Returns a borrowed book for the authenticated user. Requires session cookie.
  - **Path Parameters:**
    - `book_id` (int): Book identifier


### 12. Get Borrowed Items

- **POST /items/borrowed**
  - Returns a list of active borrowed items for the authenticated user. Requires session cookie.


### 13. Logout

- **GET /logout**
  - Logs out the user by deleting the session cookie.
  - **Parameters:** None

---

## Admin Endpoints

All `/admin/*` routes require two headers: `X-Admin-Internal-Secret` and
`Authorization: Bearer <admin token>`. Blocked from external access at the
nginx layer — only reachable from the admin UI calling the API directly on
the internal Docker network.

### Item Management

- **PATCH /admin/items/{book_id}**
  - Updates DRM/loan-duration flags and/or renames an item's OpenLibrary
    edition (fixes a wrong-edition import — moves the underlying S3 files to
    the new key). All fields optional; at least one required.
  - **Body (JSON):**
    - `encrypted` (optional, bool)
    - `loan_duration_days` (optional, int ≥ 0, or `null` to clear the
      override back to the global default). `0` means never expire, same as
      the global setting. A positive value greater than the global max
      (`GET /admin/settings/loan-limits`) is rejected with `400`.
    - `openlibrary_edition` (optional, positive int): renames the item
  - Errors: `400` invalid field, `404` not found, `409` target edition
    already exists, `500` S3/DB error during rename

- **POST /admin/items/{book_id}/reupload**
  - Replaces an item's file (fixes a wrong-file import). Item id and loan
    history are untouched — only the S3 object(s) and `encrypted`/`formats`
    change.
  - **Form Data:** `file` (required, EPUB, max 100MB), `encrypted`
    (optional bool, default false)
  - Response: plain text `"File replaced successfully."`, not JSON.

- **DELETE /admin/items/{book_id}**
  - Removes an item from S3 and the database (loans cascade). `book_id`
    accepts a bare OLID or an edition key (`OL51008637M`).
  - `204` on success, `404` if not found.

- **POST /admin/items/delete**
  - Bulk delete, up to 200 per request.
  - **Body:** `{"book_ids": [...]}` — bare OLIDs or edition keys, mixed OK.
  - Always `200`, with a per-item breakdown so one bad id never masks the
    rest: `{"deleted": [...], "not_found": [...], "failed": {...}, "invalid": [...]}`

### Loan Management

- **GET /admin/loans**
  - Filtered, paginated, sorted loan listing.
  - **Query Parameters:** `limit`, `offset`, `status` (`all` | `active` |
    `returned` | `overdue`), `user` (hex prefix of the patron email hash),
    `sort` (`borrowed_at` | `due_at` | `returned_at`), `order` (`asc` | `desc`)
  - Response: `{"items": [...], "total": <int>, "limit": <int>, "offset": <int>}`

- **POST /admin/loans**
  - Manually grants a loan, reusing the same row-locking/availability/
    idempotency as self-serve borrow. No email is sent — the intended flow is
    to copy the public borrow link
    (`{apiBase}/v1/api/items/{olid}/borrow`) to the patron, who signs in with
    the existing OTP flow, which already grants access to a loan that exists
    for their email.
  - **Body (JSON):** `openlibrary_edition` (int, required), `email` (str, required)
  - `201`: `{"id", "item_id", "openlibrary_edition", "due_date"}`
  - Errors: `400` invalid input / open-access item, `404` item not found,
    `409` no copies available, `403` patron loan limit reached

- **POST /admin/loans/{loan_id}/return**
  - Force-returns a loan by id, no body. `200` on success, `404` if the loan
    doesn't exist.

---

### Bulk Import Batches

State for the bulk EPUB import page (drop a folder, match to Open Library, review,
import). Code lives in `lenny/core/bulk_import/`. EPUB files only.

A batch is **kept for 30 days from its last write** (reads never extend it) and
is then purged, lazily, the next time a batch is created or listed. An expired
batch answers `410`; a purged one `404`. A row is identified by its path, size
and modified time, so re-dropping the same folder re-attaches to the existing
rows instead of duplicating them.

- **POST /admin/import/batches** — create a batch (`201`).
- **GET /admin/import/batches** — unexpired batches, newest first.
- **GET /admin/import/batches/{id}** — summary (also `match`: the newest match job `{status, total, done, error, retry_after}`, or `null`; per process): `total`, `counts` per state, `ready`, `auto_picked`, `skipped`, `encrypted`, `expires_at`.
- **DELETE /admin/import/batches/{id}** — clear it (`204`).
- **POST /admin/import/batches/{id}/sync** — compare the saved list with the live library: `{reverted, released, removed_from_library}`. `reverted` rows were `duplicate` but their book has since been deleted, so they are `matched` again; `released` are old imported rows whose key was freed so a re-dropped file becomes a new row; `removed_from_library` counts imported rows whose book is gone. A drop does this by itself. Every row in `GET /rows` carries `in_library` (is its book in the library right now).
- **PUT /admin/import/batches/{id}/rows** — `{"rows": [{path, size, mtime, isbn?, isbns?: [up to 5 more ISBNs the file mentions, e.g. the print edition in dc:source], title?, author?, language?, publisher?, year?, state?: "not_epub"}]}`, at most 500 per call. Idempotent. Returns `{created, existing, existing_states}`: `existing` rows were already in the list (a folder dropped again), and `existing_states` counts them by state (`{"imported": 3, "matched": 2}`), so the page can say what was already there.
- **GET /admin/import/batches/{id}/rows** — `?state=&exclude_state=&q=&skipped=&auto_picked=&staged=&order=&limit=` (limit up to 200) plus either `cursor=<last id>` (what the import passes use) or `offset=<n>` (numbered pages; not together). Returns `{rows, next_cursor, total}`; `total` is the number of rows matching the filters, ignoring paging, and is computed only when `offset` is given or `total=true` is passed (otherwise `null`), since it costs one count. `exclude_state` leaves one state out (the Loaded tab: `exclude_state=imported`) and combines with every other filter and with `total`. Order is by row id, or `order=imported_at_desc` (newest import first, id breaks ties; the history tab: `state=imported&order=imported_at_desc&offset=…`; `cursor` only works with the id order). Pages are stable. Rows carry `imported_at` (ISO time, `null` until imported).
- **PATCH /admin/import/batches/{id}/rows/{row_id}** — `{skipped?, olid?, encrypted?}`. A new `olid` is looked up on Open Library first (`502` if it cannot answer, `400` if it has no such edition; the row is then unchanged) and the answer replaces the row's `match_title`, `match_author` and `match_year`, with `decision: "user"`, `auto_picked: false` and `confidence: 0`; a staged file stays valid, it is not tied to the edition. A new `olid` on an already imported row moves the stored book to that edition; loans stay with it.
- **DELETE /admin/import/batches/{id}/rows/{row_id}** — remove the row from the batch (an imported book stays in the library); returns the batch summary. `404` if the row is gone, `409` while it is being imported. A removed row comes back if the same file is dropped again.
- **PUT /admin/import/batches/{id}/rows/{row_id}/staged** — multipart `file`: keep the book's file on the server (a matched, unskipped row; EPUB checked like any import; `400` otherwise, `413` too large). The row comes back with `staged: true`; the summary counts `staged` and `unstaged` (matched, not skipped, file not sent yet). Send files as rows match, then importing a staged row needs no browser at all, so a reload, a backgrounded phone browser or a lost folder handle loses nothing. Staged copies (`import-staging/{batch}/{row}.epub` in the bookshelf bucket) are deleted with their row, their batch, or when the batch expires.
- **POST /admin/import/batches/{id}/actions** — `{action: "skip"|"unskip"|"set_encrypted"|"remove", value?, state?, q?, skipped?, auto_picked?}` applied to every row matching the filter. `exclude_state` (one state) works with every action (Start over: `{action: "remove", exclude_state: "imported"}` clears the loaded rows and keeps the history). `remove` also accepts `row_ids: [1–500 ids]` (the rows a person ticked; combines with any filter, rows of other batches are ignored, importing rows stay). Returns `{updated}`.

**Matching and importing** (same admin credentials). Matching runs on one background
thread per process, paced to Open Library's limit (1 request/s, 3/s when
an Open Library account is signed in), ISBNs fifty to a query. Results are saved to rows as
they arrive, so polling `GET /admin/import/batches/{id}` shows partial progress
and a restart loses nothing (unmatched rows stay `queued`). Matching state is per process, so keep one worker for big imports or Open Library sees one paced stream per worker. Rows the admin has
decided (picked an edition) or skipped are never overwritten. A row without an ISBN is searched by a widening ladder (title + first author, title alone, title without subtitle, then what the file name says), so a long author list or a publisher in the author field does not empty the search; identical rows are looked up once and non-empty answers are cached for 30 minutes (a Re-check bypasses the cache); if Open Library rate limits us with a wait over 20 seconds the job fails at once with `error` and `retry_after` instead of sleeping, and the rows stay `queued` for a retry; titles compare forgivingly (subtitles, `Last, First`, bracketed noise like `(Z-Library)`). A title guessed from the file name alone goes to review, never auto-picked. Rows without an ISBN are looked up ten to a request, a few requests at once (2 in flight, 4 when an Open Library account is signed in), and only the rows that batch cannot settle get their own searches. The file's language, publisher and year also count: a different-language edition scores lower, matching publisher/year break ties between editions of the same book. The candidates list adds the editions of the best two works, so the right edition (not just the one search showed) can be picked.

- **POST /admin/import/batches/{id}/match** — `{state?: "queued"|"no_match"|"needs_review", row_ids?: [≤500]}` → `202` job `{id, status: queued|running|done|failed, total, done, error}`. `state: "no_match"` is the *Re-check* button. One active job per batch; asking again returns it.
- **POST /admin/import/batches/{id}/import** — import every ready row whose file is already on the server, in the background (`202`); closing the page does not stop it. Body (optional): `{retry_failed: true}`. Returns a job `{id, status: queued|running|done|failed, total, done, imported, duplicate, failed, waiting, error}` where `waiting` is ready rows whose file has not reached the server yet (start again once they have). One active job per batch (asking again returns it). Progress is also in the batch summary: `counts.importing` / `counts.imported` and `import` (the newest job, or `null`; per process).
- **GET /admin/import/jobs/{job_id}** — progress of a match or import job (`404` after a restart; the batch summary says the same).
- **GET /admin/import/match/{job_id}** — job progress (`404` after a restart; use the batch summary then).
- **GET /admin/import/batches/{id}/rows/{row_id}/candidates** — `{candidates: [{olid, title, authors[], year, publisher, language, isbn, score}]}`, best first, live from Open Library. `502` if Open Library is unreachable.
- **GET /admin/import/batches/{id}/rows/{row_id}/search?q=** — the "find it yourself" box in the Change edition panel: `q` is words (title, author, both), an ISBN, or an Open Library edition key or link (`OL123M`, `openlibrary.org/books/OL123M/…`). Same `{candidates}` shape as above, best match to the row first. Candidates (here and in `/candidates`) also carry `in_library`. `400` under 2 characters, `502` if Open Library is unreachable.
- **GET /admin/import/editions/lookup?key=OL123M** — checks a pasted edition key: `{olid, title, publisher, year, isbn, in_library}`; `400` bad key, `404` unknown to Open Library.
- **POST /admin/import/check-duplicates** — `{olids: [≤1000]}` → `{existing: [...]}` (already in the library).
- **GET /admin/import/limits** — `{max_rows_per_call, isbn_batch, ol_requests_per_second, ol_identified, ol_logged_in, max_file_mb, upload_concurrency, retention_days, formats}`.
- **POST /admin/import/batches/{id}/rows/{row_id}/file** — multipart `file`; imports a matched, unskipped row. The answer is the row; its `state` is the outcome: `imported`, `duplicate` (already in the library), `not_epub`, `failed`. Retrying is safe; a second attempt while one is in progress gets `409` (an abandoned attempt is retaken after 15 minutes). Runs under the same bounded upload pool as other uploads.

Row states: `queued → matched | needs_review | no_match`; `duplicate`; `importing → imported | failed | not_epub`.
`matched` rows carry `olid` and `auto_picked`; `needs_review` rows have no `olid` until the admin picks one.
### Preloaded Books

Browse and load the free Standard Ebooks Lenny can download. The catalog is what is
actually downloadable (190 books, about 195 MB), not everything Open Library lists
(864); `known` reports the larger number. All routes are admin-only.

- **GET /admin/preload/catalog** — `?q=&collection=&hide_owned=&sort=title|added_desc&new_only=&cursor=&limit=` → `{books: [{olid, title, authors[], year, cover_url, subjects[], source, source_id, size, in_library}], total, available, known, in_library, next_cursor}`. `q` matches title, author and subject words. Each book has `added_at` (when it was added to the archive.org item, `null` for books from other items) and `is_new` (added in the last 14 days); the response adds `new` (how many), `updated_at` (the item's last update) and `refreshing`. `502` if Open Library / archive.org cannot be reached and nothing is cached.
- **GET /admin/preload/books/{olid}** — one book's details for a sheet: the catalog fields plus `publisher`, `publish_date`, `pages`, `isbn`, `languages[]`, `description`, `all_subjects[]` (from Open Library, cached 6 hours), `cover_url_large`, `in_library`, `is_new` and `links` (`open_library`, `standard_ebooks`, `archive_org`). `details_loaded: false` means Open Library did not answer and only the catalog fields are there. `404` if the book is not in the catalog.
- **GET /admin/items** and **GET /admin/items/search** — every item now also carries `available_copies` and `num_lendable_total` (one query for the page).
- **GET /admin/items/{olid}/metadata** — the same details for any book in the library (not only free ones), same shape as `/admin/preload/books/{olid}`; for a book that is not a Standard Ebooks title `source_id`, `size`, `added_at`, `links.standard_ebooks` and `links.archive_org` are `null`. `details_loaded: false` when Open Library did not answer; `404` if the book is not in the library.
- **POST /admin/preload/refresh** — look at archive.org again now (`202 {started}`). The catalog also follows the item by itself: it checks the item's last-updated stamp (one tiny request, at most once a minute, off the request path) and rebuilds in the background when it changed, so books uploaded to `lenny-open-access-preloads` appear within about a minute with no change on our side. The old list stays visible until the new one is ready.
- **GET /admin/preload/collections** — `{collections: [{id, name, count, in_library}]}` (Classic fiction, Science fiction, Mystery, Poetry, …; empty ones are left out).
- **POST /admin/preload/import** — `{olids?: [int], source_ids?: [str], collection?: str}` (at most 200 listed ids; a collection can be larger) → `202 {queued, already_in_library, unknown, size}`. Books already in the library are skipped before downloading. Loads run in the background one at a time; per-book progress (with titles) is in `GET /admin/imports`. `409` while a load is still running.

`make preload items=N` still works and shares the same downloader.

### Library Links

- **GET /admin/library/links** — copyable links for sharing this library, admin only: `opds_feed`, `opds_search` (replace `{query}`), `opds_book_template` (replace `{olid}` with a book's edition number), `reader_url` (`<base_url>/`, this node's built-in reader), `base_url`, `source` and `is_public`. They are built from the address the owner configured (`LENNY_PROXY` in `.env`, e.g. `https://lender.briet.app`, else `LENNY_HOST`/`LENNY_PORT`), the same one the feed and the OAuth metadata advertise, never from the request. `is_public: false` means that address only works on this machine or network (localhost, a private IP, `.local`): set `LENNY_PROXY`.

## Authentication
Most endpoints require a valid session cookie. Use `/authenticate` to obtain one via email and OTP.

## Error Handling
Endpoints return appropriate HTTP status codes and error messages for unauthorized access, invalid input, and server errors.
