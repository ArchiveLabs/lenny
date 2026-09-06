# OAuth 2.0 — letting another service act for a patron

Lenny is an OAuth 2.0 **authorization server** and **resource server**. A
consumer — Open Library, another catalogue, a reading app — is a *client* acting
on a patron's behalf. The patron is the resource owner.

This is separate from the `/oauth/*` OPDS routes, which native OPDS readers
speak. Nothing here changes those.

> Tracking issue: [#209](https://github.com/ArchiveLabs/lenny/issues/209).
> Every alternative considered and rejected is recorded there.

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

`scripts/mock_openlibrary.py` is a working consumer that does all of this — the
shortest way to understand the client side:

```bash
python scripts/mock_openlibrary.py --lenny https://your-node.example.org \
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

## Security properties

Each of these is pinned by a test named `test_attack_*`; a green-to-red there
means a defence was removed, not that a refactor went wrong.

- **PKCE S256 required.** `plain` is refused. Verifiers must match the RFC 7636
  §4.1 grammar — the 43-character floor *is* the entropy requirement that makes
  an intercepted code unusable.
- **Authorization codes are single-use**, 60 seconds, and claimed atomically.
  Reuse revokes every token descended from that code (RFC 6749 §4.1.2).
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

Access tokens are deliberately **not IP-bound**, unlike session cookies
(`core/auth.py`): a consumer calls from its own servers, so the address
presenting the token is never the patron's.

### Lifetimes

| | |
|---|---|
| Authorization code | 60s, single use |
| Access token | 1 hour |
| Refresh token | 90 days, rotating |
| Consent handle | 10 minutes |

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

  The grace period is deliberate. A code's expiry is not the end of its
  usefulness: reuse detection reads a *spent* code to revoke the tokens it
  produced, so deleting it early would turn a detected replay into a plain
  "invalid code". Token rows survive until their refresh token dies too, since
  an access token expires in an hour while its refresh token lives ninety days.

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
make oauth2-clients                     # client ids are server-generated
make oauth2-disable CLIENT=<client_id>
```

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
