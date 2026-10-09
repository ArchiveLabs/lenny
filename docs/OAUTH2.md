# OAuth 2.0 — letting another service act for a patron

Lenny is an OAuth 2.0 **authorization server** and **resource server**. A
consumer — Open Library, another catalogue, a reading app — is a *client* acting
on a patron's behalf. The patron is the resource owner.

This is separate from the `/oauth/*` OPDS routes, which native OPDS readers
speak. Nothing here changes those.

> Tracking issue: [#209](https://github.com/ArchiveLabs/lenny/issues/209).
> Every alternative considered and rejected is recorded there.

## Two different things called "OAuth"

Lenny sits in the middle of two separate OAuth relationships. They point in
opposite directions and are configured in different places:

| | Who is the client? | Who is the provider? | Where an admin sets it up |
|---|---|---|---|
| **Patron sign-in provider** (external OIDC) | Lenny | Clerk, Google, Auth0, … | Admin → Settings → *Patron Sign-in Provider* |
| **App access** (this document) | A reading app, catalogue or Open Library | Lenny | Admin → Settings → *App Access*, or `make oauth2-register` |

The first decides **how patrons prove who they are to Lenny**. The second
decides **which apps may use Lenny's sign-in** to see a patron's loans and
borrow for them. Turning one on does not turn the other on, and an app can use
App Access whichever sign-in method the node uses.

```
 reading app  ──asks──▶  your library (Lenny)  ──asks──▶  sign-in provider
 (client)                (provider here,                  (Clerk, Google…,
                          client there)                    or one-time codes)
```

---

## Why it exists

Lenny stores "logged in" as a cookie on its own domain. A consumer cannot POST
to Lenny and receive a usable session — `Set-Cookie` lands on the consumer's
HTTP client, not the patron's browser, and the cookie is `Domain`-scoped,
`HttpOnly`, `SameSite=Lax`. So a consumer has no way to answer *"what does this
patron have on loan?"*

Every shortcut is worse than the standard:

| Shortcut | Why not |
|---|---|
| Forward the patron's IA S3 keys | Expands the blast radius of a credential not scoped to lending |
| A service key that reads any patron's loans | One leak exposes every patron's reading history |
| The consumer signs identity assertions | Across many nodes, one key compromise is a federation-wide breach |
| The node pushes a token into the consumer | Unsolicited; the consumer cannot verify the patron consented |

Authorization Code + PKCE avoids all of them. **Every token is one patron's,
granted by that patron, and never travels through the browser.**

---

## Endpoints

| | |
|---|---|
| `GET /.well-known/oauth-authorization-server` | RFC 8414 metadata (site root, not under `/v1/api`) |
| `GET,POST /v1/api/oauth2/authorize` | authorization request + patron consent |
| `POST /v1/api/oauth2/token` | `authorization_code`, `refresh_token` |
| `POST /v1/api/oauth2/revoke` | RFC 7009 |
| `GET /v1/api/oauth2/loans` | scope `loans:read` |
| `POST /v1/api/oauth2/borrow` | scope `borrow` |

### Scopes

| Scope | What the patron is told |
|---|---|
| `loans:read` | See which books you have on loan |
| `borrow` | Borrow and return books on your behalf |

An unknown or unregistered scope is an **error**, not a silent narrowing — a
client learns immediately rather than discovering a missing permission at call
time.

---

## The flow

```
0. operator runs `lenny oauth2-register …`          once per consumer, per node
1. GET  /.well-known/oauth-authorization-server     discover the endpoints
2. →    /oauth2/authorize?…&code_challenge=…        patron logs in, consents
3. ←    <redirect_uri>?code=…&state=…               code comes back
4. POST /oauth2/token   (client secret + verifier)  back channel → tokens
5. GET  /oauth2/loans   Authorization: Bearer …     use them
```

Steps 1–3 happen once per patron. After that the consumer holds a refresh token
and needs no redirect and no OTP.

`tests/oauth2/mock_openlibrary.py` is a working consumer that does all of this
— the shortest way to understand the client side. It lives under `tests/`
rather than `scripts/` because it is a harness, not an operator tool: it
pretends to be Open Library, against a node you are already running.

```bash
python tests/oauth2/mock_openlibrary.py --lenny https://your-node.example.org \
  --client-id "$ID" --client-secret "$SECRET" \
  --session "$COOKIE" --edition 37044497
```

---

## Who may connect

**The operator decides.** A consumer cannot register itself; there is no public
registration endpoint, and the metadata does not advertise one.

Open Library is the consumer nearly every node wants, so it has its own command:

```bash
make ol-connect          # prints the client_id and secret, once
make ol-disconnect       # revokes its access and every token it holds
```

Both are safe to run twice. `ol-connect` on an already-connected node reports
that and changes nothing; on a disconnected one it restores the same client, so
the secret you were given still works. `ROTATE=1` issues a new secret and
retires the old registration along with its tokens — which is what you want
after a leak, and why it is not the default.

Anything else registers explicitly:

```bash
make oauth2-register NAME="Some Consumer" URI=https://example.org/callback
```

An app that ships with a fixed client id (most reading apps do) is registered
with that id, so the app needs no change:

```bash
make oauth2-register NAME="Some Reader" URI=https://reader.example.org/ CLIENT_ID=some-reader PUBLIC=1
```

An id is 3-64 letters, digits, `.`, `_` or `-`. It is unique case-insensitively
(`Some-Reader` cannot sit beside `some-reader`) and is never reusable, even after
the client is disabled, so a retired id cannot be reclaimed to inherit its trust.
The id is public; the registered redirect URLs are what bind a client.

(The installed `lenny` command forwards to `make`, so `lenny ol-connect` works
too.)

The client id and secret are printed once — only the secret's hash is stored —
and handed to the consumer out of band.

This is deliberate. Many organisations will run Lenny nodes, but the federation
is asymmetric: the *nodes* are the long tail, while the consumers are a small,
known set. A new node operator connecting to Open Library knows that is what
they want; it is a deliberate act at setup, which is exactly the shape config
and a CLI fit. Open registration would solve the other direction — a consumer
discovering a node nobody told it about — which is not the problem this
ecosystem has.

It also buys something concrete: with only operator-blessed clients, *"Open
Library wants access to your library account"* is a verified fact rather than a
claim, so consent phishing is not possible. The consent screen still shows the
redirect host, because a compromised or mistaken registration is still worth
seeing.

Native apps register the same way, as public clients:

```bash
make oauth2-register NAME="Thorium" URI=opds://authorize/ PUBLIC=1
```

---

## Connecting a reading app

### Book Server is built in

Every node registers Internet Archive's **Book Server** (reader.archive.org) the
first time it starts: client id `reader-archive-org`, a public client, redirect
URL exactly `https://reader.archive.org`. Nothing to do on a new node.

If an operator disables it, it stays disabled; a restart never recreates it.
`make bookserver-connect` shows its state, and `ENABLE=1 make bookserver-connect`
turns it back on. This is a deliberate trust decision made once for every node:
it means whoever controls `reader.archive.org` can receive authorization codes
for patrons who approve it. A node that does not want that disables it.

### Adding any other app

Three ways, all producing the same record:

1. **Admin page.** Settings → **App Access** → *Add an app*. Name, redirect URLs
   (one per line), an optional client id, Public or Server type, and which
   permissions it may ask for. For a Server app the client secret is shown once.
   The *For developers* tab shows the node's endpoints to hand to the app's
   developer.
2. **Make.** `make oauth2-register NAME=… URI=… [CLIENT_ID=…] [SCOPE=…] [PUBLIC=1]`.
3. **Admin API.** `GET/POST /v1/api/admin/oauth2/clients`, and `POST
   …/{client_id}/disable` or `…/enable`, behind the same internal secret and
   admin token as every `/admin` route. Nginx refuses `/v1/api/admin` from the
   public side.

**What an app's developer sends the admin:** the app's name; the exact redirect
URL(s) (`https://…`, a reverse-DNS or `opds://` scheme, or `http://` on
loopback for development); whether it can keep a secret (a server-side app can,
a browser or mobile app cannot); the client id, if it ships with one; and the
permissions it needs (`loans:read`, `borrow`).

**What the admin sends back:** the client id, and for a Server app the secret
(once). The app then talks to the endpoints under *Endpoints* above, using
Authorization Code with PKCE (S256). PKCE is mandatory for every client.

Redirect URLs are matched exactly. They must not contain whitespace, a
backslash, a fragment, or `user@host` (a URL that names a different host than it
appears to), and are limited to 2048 characters. A client has a name of 1-100
characters and at most 10 redirect URLs.

### Fixing a mistake

An app registered with a wrong name, redirect URL or permission can be edited
(Admin → App Access → the app's *Edit*, or `PATCH /v1/api/admin/oauth2/clients/{id}`).
The client id and the app type are fixed, because tokens are keyed on the id and
the type decides whether a secret exists.

- **Taking a permission away signs that app's patrons out** (their live tokens are
  revoked), so the change is true immediately instead of when each token expires.
  Adding a permission, or changing the name or redirect URLs, does not.
- **A lost or leaked secret** is reset, not recovered: *Reset secret* (or `POST
  …/{id}/rotate-secret`) returns a new one once and the old one stops working at
  once. Only server apps have a secret to reset.
- **Removing an app** (`DELETE …/{id}`, or `make oauth2-delete CLIENT=<id>`) is two
  steps on purpose: turn it off first, then remove it. It deletes the app with the
  tokens and codes it held, so a later app registered under the same id inherits
  nothing. A built-in app (Book Server) cannot be removed, only turned off, because
  it would be recreated at the next start.

### What an app can do with its token

| Call | Needs | Returns |
|---|---|---|
| `GET /v1/api/shelf` | `loans:read` | the patron's current loans |
| `GET /v1/api/profile` | `loans:read` | loan counts. No name or email: a token holds only a hash of the address |
| `GET /v1/api/oauth2/loans` | `loans:read` | the patron's loans, JSON |
| `GET /v1/api/items/{id}/borrow` | `borrow` | borrows the item; same limits as any borrow |
| `POST /v1/api/oauth2/borrow` | `borrow` | borrows the item, JSON |

Send it as `Authorization: Bearer <access token>`. A token with only
`loans:read` cannot borrow. A token only ever sees its own patron's loans.
Lending rules (per-patron limit, copies available, borrowing the same book twice
is a no-op) are enforced by the same code as for a cookie login. Returning a book
and reading it still need the patron's login cookie.

An app may send `scope=openid` (or `profile`, `email`, `offline_access`); these
carry no permission here and are ignored, so a generic OIDC client works without
change. Only identity scopes is the same as asking for no scope: the client's
registered permissions. Any real scope the client was not registered for is
still refused.

### Which sign-in document do apps see?

The OPDS Authentication Document (`GET /v1/api/oauth/implicit` or
`GET /v1/api/oauth/authentication`, which serve the same document, and the 401
bodies from protected routes) follows the admin's active mode, one flow at a
time:

The document's `id` is its own address. It stays `…/oauth/implicit` while the
document advertises the implicit flow, so readers that stored it keep working, and
becomes `…/oauth/authentication` when it advertises PKCE only. The library
(`pyopds2_lenny`) chooses it; both addresses always answer.

| Admin mode | Document advertises |
|---|---|
| One-time code (`ol`), `none` | the implicit flow, exactly as before |
| External provider (`external`) | Authorization Code + PKCE only |

A node that never configured an external provider never advertises PKCE.
Advertising both flows together is deferred until the revised OPDS
authentication spec settles ([#237](https://github.com/ArchiveLabs/lenny/issues/237)).

### What a patron sees

The consent screen, on a laptop and on a phone, and the page shown when an app is
not set up with the library:

<p>
  <img src="images/oauth2-consent.png" alt="Consent screen: which account is signing in, what the app will be able to do, where the patron returns to, and Allow / Not now" width="330">
  <img src="images/oauth2-consent-phone.png" alt="The same consent screen on a phone" width="290">
  <img src="images/oauth2-error.png" alt="Error page: this app isn't set up with this library, nothing was shared, ask your librarian" width="330">
</p>

### Signing in as a different account

A browser keeps two logins: Lenny's own cookie (a week) and the sign-in
provider's session. Logging out inside an app clears neither, so the next request
would go straight to the consent screen as the same patron.

- The consent screen shows which account is signing in, with a **"Not you? Use a
  different account"** button.
- An app can ask for the same thing with the standard `prompt=login` or
  `prompt=select_account` parameter on `/oauth2/authorize`. Lenny ignores the
  existing login, drops the cookie, sends the patron through sign-in again, and
  passes `prompt` on to an external provider so its account chooser shows. Any
  other `prompt` value is ignored and never forwarded.

### When something is wrong

An app that is unknown, turned off, or sends a return address that is not
registered gets `400` with `{"error": "invalid_client"}` (or
`invalid_request`). A browser gets a short page instead of JSON: it says the app
is not set up with this library, that nothing was shared, and that the librarian
can fix it under Settings → App Access. A turned-off app looks identical to an
unknown one, so the response does not reveal which client ids exist. No redirect
is made on these errors (RFC 6749 §4.1.2.1).

### Where it is stored

In the Lenny database: the `oauth_clients`, `oauth_authorization_codes` and
`oauth_access_tokens` tables. Nothing is written to `.env` or `auth.env` (the
external sign-in provider's own settings are the only OAuth values kept there).
A client secret, authorization codes, and access and refresh tokens are stored
as SHA-256 hashes: a database dump yields nothing replayable. The default
client's definition (id, name, redirect) is in code and contains no secret.

### Things worth knowing

- **Sessions outlive a mode switch.** Lenny's login cookie does not record how it
  was obtained. A patron who signed in by one-time code keeps working after the
  admin switches to an external provider, until the cookie expires or they choose
  *Not you?*. They are the same patron either way (same address).
- **`prompt=login` clears the cookie on a GET.** Any site can link a patron to
  `/oauth2/authorize?…&prompt=login` for a valid client and so sign them out of
  Lenny. It costs a patron a sign-in and nothing more; it is the standard
  behavior of the parameter.
- **A public tunnel exposes the node.** `make tunnel` publishes the whole site.
  The admin API stays blocked at nginx and the admin page needs credentials, but
  close the tunnel when you are done (`make untunnel`).

---

## Upgrading to the app-access release

Written for an operator who has a node that works today and wants to stay that
way. **There is no database migration and no new required setting.** Rolling back
is safe; it leaves one extra row in `oauth_clients`, which nothing else reads.

| What changes | Who it can affect | What to do |
|---|---|---|
| **Book Server is registered automatically** at startup (`reader-archive-org`, redirect `https://reader.archive.org`) | Every node. It can now ask a patron for access, and the patron still has to approve it on the consent screen | To opt out: `make oauth2-disable CLIENT=reader-archive-org`. It stays off across restarts. A client you already registered under that id is left untouched |
| **In `external` provider mode the sign-in document shows PKCE only** (it used to show implicit) | Nodes in `external` mode whose readers only speak the implicit flow (older Thorium, for example). They stop seeing a flow they can use | Add `LENNY_AUTH_DOC_MODE=both` to `.env` and restart the API. The document then lists implicit first, then PKCE, in any mode. Nodes in `ol` or `none` mode see no change |
| `/shelf`, `/profile` and `/items/{id}/borrow` **also accept an app's bearer token** | Nobody. Cookie logins behave as before | None |
| Consent screen and sign-in error page redesigned | Patrons see a clearer page. No flow changes | None |
| `prompt=login` / `select_account` and a **"Not you?"** button | Additive | None |
| The public API documentation **no longer lists admin routes** | Anyone who scraped `/openapi.json` for them. The routes themselves are unchanged | None |
| **Admin: a new *App Access* screen** (Settings → Apps & readers) | Needs the matching `lenny-app` build | Upgrade both together. Lenny without the new admin works, the screen is simply missing. The new admin with an older Lenny shows an error on that screen only |
| **The admin app is hardened** (the matching `lenny-app` build): its proxy rejects odd paths, answers 401 without the admin cookie, checks `Origin` on changing calls, and sign-in now fails closed in production when its two settings are missing | Anyone running the admin behind their own reverse proxy, or running `lenny-app` outside Lenny's `compose.yaml` | Nothing with the bundled `compose.yaml` and nginx: they already pass `LENNY_INTERNAL_API_URL` and `ADMIN_INTERNAL_SECRET` and forward `Host`. A custom proxy must forward `Host` or `X-Forwarded-Host`, and Secure cookies need TLS in production |
| `pyopds2_lenny` is pinned to a newer commit | Nobody | With an older copy the sign-in document keeps showing the implicit flow |

### To start using OAuth PKCE with your apps

1. Make sure the node is reachable at its public address (`LENNY_PROXY`, or the
   host and port from `make configure`). Every URL an app is given is built from it.
2. Add each app: Settings → **App Access → Add an app**, or `make oauth2-register`
   (see *Connecting a reading app*). Book Server is already there.
3. Give the app's developer the endpoints from the *For developers* tab (or
   `/.well-known/oauth-authorization-server`).
4. If readers find your node through its OPDS feed rather than being configured by
   hand, decide what the sign-in document should advertise: one flow at a time (the
   default) or both (`LENNY_AUTH_DOC_MODE=both`).

Nothing here changes how patrons sign in today. Whether they use one-time codes or
an external provider is still the admin's choice, and is a separate setting from
which apps may use Lenny's sign-in.

### Settings added by this release

| Setting | Default | Meaning |
|---|---|---|
| `LENNY_AUTH_DOC_MODE` | `active` | `active`: the sign-in document shows only the flow the admin switch selected. `both`: it shows implicit, then PKCE, in every mode |

### Not done in this release

Returning a book and opening it in the reader still require the patron's own
login cookie, so an app that holds only a token cannot do either. Showing both
flows by default, Device Authorization, a domain allow-list for the sign-in hint,
and a client registry are tracked in
[#237](https://github.com/ArchiveLabs/lenny/issues/237) because the revised OPDS
authentication spec is not final. A login cookie also does not record how it was
obtained, so a one-time-code login keeps working after the admin switches to an
external provider until it expires.

---

## Security properties

Each of these is pinned by a test named `test_attack_*`; a green-to-red there
means a defence was removed, not that a refactor went wrong.

- **PKCE S256 required.** `plain` is refused. Verifiers must match the RFC 7636
  §4.1 grammar — the 43-character floor *is* the entropy requirement that makes
  an intercepted code unusable.
- **Authorization codes are single-use**, 60 seconds, and claimed atomically.
  Reuse revokes every token descended from that code (RFC 6749 §4.1.2) — and
  `issue` locks the grant row while it mints, so the revocation and the minting
  cannot interleave. Claiming atomically is only half the job: without the lock
  the winner of a race reads a snapshot taken before the loser's revocation
  committed and issues a *live* token against a grant already recorded as
  revoked, which handed roughly half of all detected replays a working token.
- **Codes are bound** to client, `redirect_uri` and PKCE challenge.
- **Redirect URIs match exactly** — no prefix, no wildcard. `https://` anywhere;
  `http://` on loopback (RFC 8252 §7.3); and private-use schemes for native apps
  (§7.1) — `opds://`, or reverse-DNS like `com.example.reader://`. A
  single-label scheme such as `myapp://` is refused: any other app on the device
  can claim it.
- **An unregistered `redirect_uri` renders an error**, never redirects to it —
  otherwise `/authorize` is an open redirector wearing an OAuth costume.
- **Refresh tokens rotate**, and reuse revokes the family (RFC 9700 §4.14.2).
- **Revocation covers the grant**, requires client authentication, and is scoped
  to that client's own tokens — while always returning 200, so it cannot be used
  as an existence oracle.
- **The consent form carries one opaque signed handle**, bound to the patron it
  was shown to. The POST cannot be fed different parameters than were displayed,
  and a handle minted by an attacker cannot be submitted by a victim.
- **Secrets are stored as SHA-256 digests** — client secrets, codes, access and
  refresh tokens. A database dump yields nothing replayable.
- **The implicit grant is absent by design.** OAuth 2.1 removes it; the
  token-in-URL exposure is what this replaces.
- **The consent screen states only what a patron can actually do.** There is no
  patron-facing disconnect yet: `/oauth2/revoke` needs the *client's* own
  credentials, and `oauth2-disable` cuts every patron off at once. So the
  screen names the operator as the remedy and the ceiling as the expiry, rather
  than implying a control that does not exist. A "connected apps" page is the
  right fix, and would make this copy wrong in the other direction — the test
  `test_consent_does_not_promise_a_revocation_that_does_not_exist` is where to
  start when someone builds it.

Access tokens are deliberately **not IP-bound**, unlike session cookies
(`core/auth.py`): a consumer calls from its own servers, so the address
presenting the token is never the patron's.

### Lifetimes

| | |
|---|---|
| Authorization code | 60s, single use |
| Access token | 1 hour |
| Refresh token | 90 days, rotating |
| **Grant (absolute)** | **1 year from the patron's consent** |
| Consent handle | 10 minutes |

The absolute grant lifetime is the one that is not obvious. Rotation renews the
refresh token's 90 days on every use, so without a ceiling a single "Allow"
click would grant access for as long as the consumer kept refreshing —
indefinitely. It is derived on every rotation from the authorization code's
`created_at`, which costs no column and no migration: the sweep keeps a code
for as long as any token references it, so the row is always there to read.
Both tokens are capped by it, not just the refresh token — otherwise a pair
minted just before the ceiling stays usable for another hour.

---

## Operating a node

Nothing to configure — the endpoints are available as soon as the node runs.
Who may use them is a separate, deliberate step: see "Who may connect" above.

### If a public proxy sits in front of the node

RFC 8414 puts the metadata document at the **origin root**, not under a path
prefix — `https://your-node.example.org/.well-known/oauth-authorization-server`.
A deployment whose public proxy forwards only `/v1/api` will therefore 404 it,
even though every endpoint the document describes is reachable. That is the
shape lennyforlibraries.org has, and it was not obvious until discovery was
tried against it.

Lenny's own bundled nginx routes it. A proxy in front of that needs one block:

```nginx
location = /.well-known/oauth-authorization-server {
    proxy_pass http://<lenny-upstream>/.well-known/oauth-authorization-server;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Until it is added, the flow still works — a consumer given the endpoint URLs
directly can complete every step. Only automatic discovery is affected, which
matters most for a consumer meeting a node it has not been configured for.

Two more things worth knowing:

- **`LENNY_PROXY` (or `LENNY_HOST`/`LENNY_PORT`) must name this node's public
  URL.** The metadata document is built from it — never from the request — so a
  node reached at an address its configuration does not know about will
  advertise endpoints nobody can reach, and log a warning saying so on every
  request.

  This is the same `LennyAPI.make_url` that builds the OPDS feed and the
  Authentication Document, so if it is wrong those are already wrong. Deriving
  the issuer from the `Host` header instead would let anyone who can set that
  header advertise an attacker-controlled token endpoint, and RFC 8414 §3.3
  makes the issuer security-relevant precisely because clients trust it.

  Running locally on a non-default port therefore needs:

  ```bash
  LENNY_PROXY=http://127.0.0.1:8097 uvicorn lenny.app:app --port 8097
  ```
- **Codes and tokens accumulate.** `lenny.core.oauth2.sweep_expired()` deletes
  rows that can no longer be used. Nothing calls it automatically — run it from
  a cron or a console:

  ```bash
  make oauth2-sweep       # safe to run from cron
  ```

  The order is deliberate: tokens go first, then only those codes nothing
  descends from any more. `revoke_for_code` needs the code row to mark a grant
  revoked, and `issue` locks that same row to serialise against it — so
  deleting a code while its tokens can still be refreshed left 9 out of 10
  refresh replays with a live token. A refresh token lives ninety days, so the
  old one-day cutoff left that open for the other eighty-nine. Token rows
  survive until their refresh token dies too, since an access token expires in
  an hour while its refresh token lives ninety days.

### Native and public clients

A reading app has no https origin and cannot keep a secret, so it registers as a
public client (`PUBLIC=1` above) and gets none — PKCE is what protects it, and
PKCE is mandatory here. Redirects may use a private-use scheme: `opds://`, or
reverse-DNS like `com.example.reader://`.

Because a browser will not reliably follow a private-use scheme, the
authorization step renders a handoff page with the link rather than issuing a
303.

Note that Lenny's older OPDS routes still advertise only the implicit flow in
their Authentication Document, so native readers reaching Lenny that way stay on
implicit for now. Moving them is a deliberate follow-up, not an oversight —
it changes what every existing reader sees.

### Stopping a client

A consumer's mandate can end, or its credentials can leak:

```bash
make oauth2-clients                     # lists every client and its id
make oauth2-disable CLIENT=<client_id>
```

The admin page's *App Access* screen does the same with a confirmation, and can
turn a client back on. Turning one back on restores the client, not the tokens it
held: its patrons sign in again from scratch.

This revokes what the client already holds as well as blocking new tokens —
otherwise "disabled" would mean "cannot get new tokens" while the ones in hand
keep working for up to an hour, and its refresh tokens for ninety days. The row
is kept rather than deleted, so the audit trail survives.

---

## Testing

```bash
pytest tests/test_oauth2_core.py tests/test_oauth2_endpoints.py
```

Concurrency and browser tests need more setup, and both exist because the
ordinary suite structurally cannot catch what they catch — see
`docs/oauth2-testing.md`. In short:

- **`tests/test_oauth2_concurrency.py`** needs real Postgres. Under SQLite every
  connection gets its own in-memory database, so two "concurrent" callers never
  contend and a double-spend is invisible. Two critical bugs hid there.
- **`tests/test_oauth2_browser.py`** needs Chromium and a running node.
  `TestClient` does not follow a 303 across origins, run a form, or carry a
  `Set-Cookie` the way a browser does.
