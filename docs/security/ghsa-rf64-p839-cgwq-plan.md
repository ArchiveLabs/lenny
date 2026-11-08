# GHSA-rf64-p839-cgwq — plan

**Private security advisory. Embargoed. Do not disclose publicly until Mek says so.**
This work lives in the temporary private fork `ArchiveLabs/lenny-GHSA-rf64-p839-cgwq`,
never in the public repo.

Advisory: "OAuth2 consent-screen CORS guard matches the path by exact equality and fails open."

## The defect(s)

`lenny/app.py` middleware `no_cross_origin_reads_of_oauth2_authorize`:

1. **Fragile route selection (primary).** Selects the route with
   `if request.url.path == "/v1/api/oauth2/authorize"` — exact equality on the
   *externally observed* path. Any deployment whose observed path differs
   (ASGI `root_path`, a mount prefix, a trailing slash, a proxy that rewrites)
   does not match, the guard silently does not apply, and nothing signals it.
   Fails **open**.

2. **The "deny" is actually a grant (secondary).** When it *does* match it sets
   `Access-Control-Allow-Origin: "null"`. `null` is a real, attacker-reachable
   origin (sandboxed iframes, `data:` documents), so the header grants
   cross-origin access to exactly that origin rather than denying all. A true
   deny is the **absence** of the header. Impact is bounded today only because
   `Access-Control-Allow-Credentials: "false"` is also set, so a credentialed
   read still fails — but one future change restoring credentials on this path
   turns it into a live cross-origin read of a cookie-authenticated page.

## Two jobs, not conflated

### (1) MEASURE current exposure (severity input; do NOT perform the harm)
- [x] Read the dispatch: `lenny/app.py` middleware + router mounts.
- [x] Read nginx: `docker/nginx/conf.d/lenny.conf`, `docker/nginx/nginx.conf`.
- [x] Read ASGI launch / `root_path`: `docker/api/Dockerfile` CMD, `FastAPI()` init.
- [x] Determine whether the observed `request.url.path` matches the literal in the
      shipped stack today, **with a control** (a case that would NOT match).
- [ ] Report measurement to the lead; lead sets severity.

### (2) FIX regardless of (1)
- [ ] Fix #1: select on the resolved route (`request.scope["route"]`), not the
      observed path string.
- [ ] Fix #2: **pop** `Access-Control-Allow-Origin` and
      `Access-Control-Allow-Credentials` (deny = absence), not set to "null"/"false".
- [ ] Test that goes RED when the guard stops applying — proven red-first then green.
- [ ] Explicit mount-prefix (`root_path`) and trailing-slash cases.
- [ ] Explicit null-origin case: header is ABSENT on the consent response.
- [ ] `make test` / lint green.

## Reporting
- Report to the Lenny Division Lead only. Stay in DRAFT. Mek merges; Mek alone
  authorizes any public disclosure.
- PR body states what was verified by RUNNING vs by READING, as two lists.

## Commit identity
All commits authored + committed as `Open Library Bot
<50213894+openlibrary-bot@users.noreply.github.com>`. Verify both before every push.
