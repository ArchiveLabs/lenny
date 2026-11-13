# API

All endpoints are under `/v1/api` on your node. The complete, current reference is generated from the code:

- Interactive docs: `http://localhost:8080/v1/api/docs`
- OpenAPI schema: `http://localhost:8080/v1/api/openapi.json`

This page lists the parts most integrations need.

## Public endpoints

| Method and path | Purpose |
|---|---|
| `GET /v1/api/health` | Returns `{"status": "ok"}`. Used by the container health check. |
| `GET /v1/api/opds` | OPDS 2.0 catalog. `offset`, `limit` (default 50), `modified_since`. |
| `GET /v1/api/opds/search?query=` | OPDS search. |
| `GET /v1/api/opds/{id}` | One publication, with borrow and read links for the signed-in patron. |
| `GET /v1/api/items` | Items as JSON. `fields`, `offset`, `limit`, `encrypted=true\|false`, `modified_since`. |
| `GET\|POST /v1/api/items/{id}/borrow` | Borrow a book. Needs a signed-in patron and an active lending mode. |
| `GET\|POST /v1/api/items/{id}/return` | Return a book. |
| `GET /v1/api/items/{id}/read` | Open the book in the reader. Needs a loan for encrypted books. |
| `GET /v1/api/shelf`, `GET /v1/api/profile` | The patron's loans and account. |
| `GET\|POST /v1/api/logout` | End the patron session. |
| `GET /v1/api/oauth/authentication` | The OPDS authentication document: which sign-in flows this node offers. |
| `GET /.well-known/oauth-authorization-server` | OAuth 2.0 metadata for apps. See [OAUTH2.md](OAUTH2.md). |

Book files are never served directly. Readium is reachable only through `/v1/api/items/{id}/readium/…`, which checks the loan first.

`POST /v1/api/upload` and everything under `/v1/api/admin` need the admin internal secret and an admin token. nginx returns `403` for `/v1/api/admin` from outside; those routes are for the admin UI container.

## Incremental harvesting

`GET /v1/api/opds` can return only what changed, so an aggregator does not refetch the whole catalog on every pass:

```sh
# everything modified on or after 1 August 2026, 100 per page
curl "http://localhost:8080/v1/api/opds?modified_since=2026-08-01&limit=100"
```

- `modified_since` takes an ISO 8601 date (`2026-08-01`) or timestamp (`2026-08-01T12:30:00Z`). No time zone means UTC. Anything else returns `400`.
- Results are ordered oldest change first, so paging is stable and a harvest can resume where it stopped.
- Follow `rel=next` for further pages; it keeps your `modified_since`.
- Each publication carries `metadata.modified`, so a consumer can advance its cursor from the feed instead of its own clock.
- `metadata.numberOfItems` counts the whole matching set, not the page.

Open Library's BookWorm harvester uses this ([internetarchive/openlibrary#13241](https://github.com/internetarchive/openlibrary/pull/13241)).

## OPDS libraries

The feed is built with Archive Labs' [pyopds2](https://github.com/ArchiveLabs/pyopds2) and [pyopds2_lenny](https://github.com/ArchiveLabs/pyopds2_lenny) packages, following [OPDS 2.0](https://opds.io/).

## OAuth 2.0 for apps

Apps that borrow on a patron's behalf use Authorization Code with PKCE and the `/v1/api/oauth2/*` endpoints. Scopes, tokens and the consent flow are in [OAUTH2.md](OAUTH2.md).
