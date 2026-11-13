# Configuration

Lenny reads its settings from five files in the checkout. `make configure` (run by `make start`) creates any that are missing, with random secrets, and never overwrites one that exists. `make update` adds settings introduced by a new release to existing files and leaves your values alone.

| File | Holds | Edited by |
|---|---|---|
| `.env` | Server, database and storage settings, `LENNY_SEED` | you |
| `auth.env` | First admin login, admin secrets, OIDC provider, IA key sign-in | you, and the admin UI (sign-in settings) |
| `ol.env` | Open Library / Internet Archive keys, lending mode | `make ol-login`, the admin UI |
| `loan.env` | Loan limit and loan length | the admin UI |
| `reader.env` | Reader settings | you, and `make tunnel` |

`.env`, `auth.env` and `ol.env` contain secrets and are created with mode `600`. Keep all five out of version control (they are in `.gitignore`).

After editing a file by hand, apply it with `make start`. Settings the admin UI changes take effect without a restart.

"Default" below is what `make configure` writes. If a setting is missing from the file, the code falls back to the value in brackets where that differs.

## `.env`

### Server

| Variable | Default | Meaning |
|---|---|---|
| `LENNY_PORT` | `8080` | Host port nginx is published on. |
| `LENNY_HOST` | `localhost` | Host name used to build public links when `LENNY_PROXY` is empty. |
| `LENNY_PROXY` | empty | Public base URL, e.g. `https://library.example.org`. Used in OPDS links, sign-in redirects and OAuth metadata. `make tunnel` sets it to the tunnel URL; `make untunnel` clears it. |
| `LENNY_SEED` | random | Signs session cookies and keys the patron identity hash. Back it up; never change it. The API refuses to start without it. |
| `LENNY_WORKERS` | `2` (`3`) | uvicorn worker processes. Use `1` on very small machines. |
| `LENNY_PRODUCTION` | `true` | `false` runs uvicorn with `--reload` and picks up code changes from the checkout. For development. |
| `LENNY_LOG_LEVEL` | `debug` (`info`) | uvicorn log level. |
| `LENNY_FORWARDED_ALLOW_IPS` | your Docker network's subnet, else `172.16.0.0/12` | Proxies trusted to set `X-Forwarded-For`. Never `*` and never empty: `*` lets a client pick its own IP, empty makes every patron share one. |
| `LENNY_OPDS_ALLOWED_HOSTS` | `reader.archive.org` | Comma-separated host names allowed as `https://` redirect targets after OPDS sign-in. Add the host of any browser-based OPDS client on another origin. See [OAUTH.md](OAUTH.md). |
| `LENNY_AUTH_DOC_MODE` | not set (`active`) | `both` makes the OPDS authentication document list the older implicit flow as well as PKCE, for readers that only know implicit. See [OAUTH2.md](OAUTH2.md#upgrading-to-the-app-access-release). |
| `NEXT_PUBLIC_API_URL` | empty | Base URL the admin UI calls from the browser. Leave empty when the admin UI is served by Lenny's own nginx. A non-empty value is compiled into the admin image, so no prebaked image matches and you must build it with `LENNY_ALLOW_LOCAL_BUILD=1`. |
| `LENNY_ADMIN_NODE_HEAP_MB` | `384` | Node.js heap cap for the admin UI container. |
| `OTP_SERVER` | `https://openlibrary.org` | Server that sends patrons their one-time passwords. |
| `BRIET_REDEEM_URL` | `https://demo.market.briet.app/api/redeem-lenny` | BRIET bundle redemption endpoint used by `make briet-redeem`. The default is BRIET's demo marketplace. |
| `LENNY_OL_TIMEOUT` | not set (`8`) | Seconds to wait for Open Library per call. After 3 failures in a row Lenny stops calling for 30 seconds and serves its last good copy. |
| `LENNY_OL_STALE_TTL` | not set (`3600`) | Seconds a last-good Open Library response may be served while Open Library is down. |
| `OL_PROVIDER_NAME` | not set | Overrides the provider name `make ol-connect` registers with Open Library (default `lenny_<your host>`). |
| `LENNY_SSL_CRT`, `LENNY_SSL_KEY` | empty | Only used when running `python -m lenny.app` outside Docker. The Docker setup ignores them; put TLS in front of nginx instead. |
| `READER_PORT`, `READIUM_PORT` | `3000`, `15080` | Internal ports of the reader and Readium on the Docker network. Not published. Leave them as they are: nginx and Readium are not configured to follow a change. |

### Database

| Variable | Default | Meaning |
|---|---|---|
| `DB_USER` | `librarian` | Postgres user. |
| `DB_PASSWORD` | random | Postgres password. |
| `DB_NAME` | `lenny` | Database name. |
| `DB_HOST` | `127.0.0.1` | Ignored in Docker: compose sets `DB_HOST=db`. |
| `DB_PORT` | `5432` | Port Postgres is published on at `127.0.0.1`, and also the port the API connects to inside the Docker network. Leave it at `5432`. |
| `DB_TYPE` | `postgres` | Informational. |

### Storage (Garage S3)

| Variable | Default | Meaning |
|---|---|---|
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | random | Garage key for the `bookshelf` bucket. |
| `S3_ENDPOINT` | `http://s3:3900` | Garage address on the Docker network. |
| `S3_REGION` | `garage` | |
| `S3_SECURE` | `false` | Use TLS to reach S3. |
| `S3_RPC_SECRET` | random (64 hex chars) | Garage RPC secret. |
| `S3_ADMIN_TOKEN` | random | Garage admin token. |
| `S3_PROVIDER` | `garage` | Used by the update engine. |
| `S3_GARAGE_MIGRATED` | `true` | Marks the one-time MinIO to Garage copy as done. |

### Memory

On a machine with little RAM, set these in `.env` and run `make start`:

| Variable | Suggestion |
|---|---|
| `LENNY_WORKERS` | `1` |
| `LENNY_ADMIN_NODE_HEAP_MB` | keep `384`; raise it only on hosts with plenty of memory |

Postgres sizes its own buffers from the host's RAM at start (`docker/utils/pg_tune.sh`).

## `auth.env`

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_USERNAME` | `admin` | Username of the first admin account. |
| `ADMIN_PASSWORD` | random | Password of the first admin account. Read once, when the admin table is created on first start; after that the database is the only source. If empty, no account is created; run `make admin-create`. |
| `ADMIN_INTERNAL_SECRET` | random | Shared secret between the admin UI container and the API. |
| `ADMIN_SALT` | random | Salt for signing admin session tokens. |
| `LENNY_EXTERNAL_AUTH_ENABLED` | `false` | Patron sign-in through your own OIDC provider. Set together with lending mode `external`. |
| `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET` | empty | Your OIDC client. |
| `OAUTH_DISCOVERY_URL` | empty | The provider's `.well-known/openid-configuration` URL. |
| `OAUTH_REDIRECT_URI` | empty | Lenny's callback as registered with the provider. |
| `OAUTH_SCOPES` | `openid email profile` | |
| `OAUTH_FLOW` | `pkce` | The only supported flow. |
| `IA_AUTH_ENABLED` | `false` | Let patrons sign in with Internet Archive S3 keys. |

The OIDC settings are explained step by step in [OAUTH.md](OAUTH.md). They can also be set in the admin UI.

## `ol.env`

| Variable | Default | Meaning |
|---|---|---|
| `LENNY_LENDING_MODE` | `none` | `none`, `ol` or `external`. See [Lending modes](admin.md#lending-modes). |
| `OL_S3_ACCESS_KEY`, `OL_S3_SECRET_KEY` | empty | Internet Archive keys, written by `make ol-login` or the admin UI. |
| `OL_USERNAME` | empty | The account those keys belong to. |
| `LENNY_OL_INDEXED` | `false` | Whether the operator chose to list this library's books in Open Library (asked by the installer). |

## `loan.env`

| Variable | Default | Meaning |
|---|---|---|
| `LENNY_LOAN_LIMIT` | `10` | Most loans one patron may hold at once. |
| `LENNY_LOAN_DURATION_DAYS` | `7` | Days until a loan returns itself. `0` means loans never expire. A book can override this. |

## `reader.env`

| Variable | Default | Meaning |
|---|---|---|
| `MANIFEST_ALLOWED_DOMAINS` | `127.0.0.1,localhost,*.trycloudflare.com` | Hosts the reader will open books from. `*.` matches any subdomain. Add your public host name here. Read at start; recreate the reader to apply (`make start`). |
| `MANIFEST_ROUTE_FORCE_ENABLE` | `true` | Compiled into the reader image. Changing it needs a local build. |
| `NODE_ENV` | `production` | |

## Shell variables for commands

These are set on the command line, not in a file.

| Variable | Used by | Meaning |
|---|---|---|
| `LENNY_ALLOW_LOCAL_BUILD=1` | `make start`, `make update` | Build the reader, admin or catalog image locally when no prebaked image matches. |
| `LENNY_NONINTERACTIVE=1` | `make ol-login`, `make ol-logout` | Skip the "replace current account?" and "log out?" confirmations. |
| `OL_EMAIL`, `OL_PASSWORD` | `make ol-login` | Credentials, instead of prompting. Prefer the prompt for the password: environment variables show up in shell history and `ps`. |
| `LENNY_DEFAULTS=1` or `-y` | `install.sh` | Answer "no" to every installer question. |
| `LENNY_PRELOAD`, `LENNY_LENDING`, `LENNY_INDEXED` | `install.sh` | `1` or `0`, answering one installer question. |
| `LENNY_REGISTRY`, `LENNY_IMAGE_ORG` | image scripts | Pull prebaked images from another registry or organisation (default `ghcr.io`, `archivelabs`). |

## Make targets

Run from the checkout, or use the `lenny` command (`lenny start` = `make start`). Targets marked "needs Lenny running" check for the `lenny_api` container first.

### Run

| Target | What it does |
|---|---|
| `make start` (also `make`, `make all`) | Create missing config, pull images, start all services. |
| `make stop` | Stop all services and close the tunnel. |
| `make restart` | Recreate the API container (reloads `.env`, `auth.env`, `ol.env`). |
| `make log` | Follow logs of all services. |
| `make doctor` | Check Docker, `.env`, database, free disk and version. |
| `make configure` | Create missing config files. Never overwrites. |
| `make update` | Update to the latest release. See [Install](install.md#update). |
| `make redeploy` | Rebuild the API image from the checkout and restart it. Keeps data. |
| `make prune` | Remove dangling images and cap the Docker build cache at 2 GB. Never touches volumes. |

### Public access

| Target | What it does |
|---|---|
| `make tunnel` | Start a Cloudflare quick tunnel to port 8080, save it as `LENNY_PROXY`, start Lenny. |
| `make untunnel` | Close the tunnel. |
| `make url` | Print the tunnel's catalog and OPDS URLs. |

### Books

| Target | What it does |
|---|---|
| `make addbook olid=OL123M filepath=/path/book.epub [encrypted=true]` | Upload one EPUB. `olid` may be `OL123M` or `123`. |
| `make preload [items=N]` | Import N books (all, about 800, if omitted) from Standard Ebooks. Needs Lenny running and `bc` on the host. |
| `make briet-redeem code=ABC123` | Redeem a BRIET bundle code and import its books. Needs Lenny running. |
| `make delete-book olid="OL123M 456"` | Delete books (files and records). Space-separated for several. Needs Lenny running. |

### Accounts and lending

| Target | What it does |
|---|---|
| `make admin-create NAME="Ada Lovelace" EMAIL=ada@example.org [USERNAME=ada]` | Add an admin. The first one becomes the super user. Prompts for a password. |
| `make admin-passwd USERNAME=admin` | Set an admin's password. |
| `make ol-login` | Log in to Internet Archive, store the keys in `ol.env`, set lending mode `ol`, restart the API. Needs Lenny running. |
| `make ol-logout` | Clear the keys, set lending mode `none`, restart the API. Needs Lenny running. |

### OAuth 2.0 apps

See [OAUTH2.md](OAUTH2.md).

| Target | What it does |
|---|---|
| `make oauth2-clients` | List registered apps. |
| `make oauth2-register NAME="…" URI=https://…/callback [SCOPE=…] [CLIENT_ID=…] [PUBLIC=1]` | Register an app. |
| `make oauth2-disable CLIENT=<id>` | Turn an app off. |
| `make oauth2-delete CLIENT=<id>` | Delete a disabled app and its tokens. Built-in apps cannot be deleted. |
| `make oauth2-sweep` | Delete expired codes and tokens. Safe to run from cron. |
| `make ol-connect [URI=…] [ROTATE=1]` | Register Open Library as an app. `ROTATE=1` issues a new secret. |
| `make ol-disconnect` | Revoke Open Library's access. |
| `make bookserver-connect [ENABLE=1]` | Check, or re-enable, the built-in reader.archive.org app. |

### Database

See [MIGRATIONS.md](MIGRATIONS.md). All need Lenny running.

| Target | What it does |
|---|---|
| `make migrate` | Apply pending migrations (they also run on every API start). |
| `make migrate-status` | Show the current revision and history. |
| `make migration msg="…"` | Generate a migration from model changes. Developers only. |
| `make migrate-rollback` | Undo the last migration. Asks to confirm. |
| `make migrate-stamp` | Mark the database as up to date without running migrations. |
| `make squash-migrations` | Replace all migrations with one baseline. Major releases only. |

### Destructive

| Target | What it does |
|---|---|
| `make build` | Delete all volumes, build every image locally (with cache), start. |
| `make rebuild` | Same, without the build cache. |
| `make resetdb` | Stop and delete all containers and volumes of project `lenny`. |
| `make teardown` | `docker compose down --volumes --remove-orphans`. |
| `make cleanup-old-s3` | Remove the old MinIO container, volume and images after the Garage migration. |

`make build`, `make rebuild`, `make resetdb` and `make teardown` delete the database **and** the stored books.
