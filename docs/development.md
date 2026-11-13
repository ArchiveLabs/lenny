# Development

## Run it locally

```sh
git clone https://github.com/ArchiveLabs/lenny.git
cd lenny
make start
```

`make start` builds the API image locally when your Python code differs from the published image, and pulls the rest. For hot reload, set `LENNY_PRODUCTION=false` in `.env` and run `make redeploy`. The checkout is mounted into the API container, so uvicorn reloads on every save. Set it back to `true` and run `make redeploy` to return to production mode.

Shell into the API container:

```sh
docker exec -it lenny_api bash
```

### Changing the reader, admin app or catalog

The reader, admin UI and catalog images are built from pinned commits of other repositories:

| Image | Dockerfile | Pin |
|---|---|---|
| `lenny-reader` | `docker/reader/Dockerfile` | `THORIUM_REF` ([edrlab/thorium-web](https://github.com/edrlab/thorium-web)) |
| `lenny-admin` | `docker/lenny-app/Dockerfile` | `LENNY_APP_REF` ([ArchiveLabs/lenny-app](https://github.com/ArchiveLabs/lenny-app)) |
| `lenny-opds` | `docker/opds-client/Dockerfile` | `OPDS_CLIENT_REF` ([ArchiveLabs/opds-client](https://github.com/ArchiveLabs/opds-client)) |

Changing a pin or a Dockerfile changes that image's content hash, so no published image matches until CI publishes one after merge. Build it locally:

```sh
LENNY_ALLOW_LOCAL_BUILD=1 make start
```

`make build` and `make rebuild` also build every image locally, but they **delete all volumes first**.

### Developing lenny-app against a local Lenny

The admin UI calls the API directly, bypassing nginx (which blocks `/v1/api/admin`). To run lenny-app's dev server against your node:

1. Publish the API port on loopback. In `compose.yaml`, under the `api` service:

   ```yaml
   ports:
     - "${LENNY_PORT:-8080}:80"
     - "127.0.0.1:1337:1337"   # local lenny-app development only
   ```

   Then `make start`.

2. In the lenny-app checkout, create `apps/web/.env.local`:

   ```env
   LENNY_INTERNAL_API_URL=http://localhost:1337/v1/api
   NEXT_PUBLIC_API_URL=http://localhost:8080
   ADMIN_INTERNAL_SECRET=<ADMIN_INTERNAL_SECRET from Lenny's auth.env>
   ```

3. Run `pnpm dev` in lenny-app and open the dev server's `/admin` directly, not through port 8080 (nginx sends `/admin` to the built container).

Remove the `1337` line and run `make start` again when you are done. Do not commit it.

## Tests

The suite runs without Docker, against an in-memory SQLite database with S3 and Open Library mocked. CI runs it on Python 3.12.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

`pytest.ini` sets the test `LENNY_SEED`. Browser tests that need a running node take `--lenny <url>` and are skipped without it. End-to-end OAuth tests: [oauth2-testing.md](oauth2-testing.md) and [tests/e2e/README.md](../tests/e2e/README.md).

CI (`.github/workflows/ci.yml`) also runs flake8 for syntax errors and undefined names:

```sh
pip install flake8
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
```

## Database migrations

Migrations live in `alembic/versions/` and run on every API start. Generate one after changing `lenny/core/models.py`:

```sh
make migration msg="add pkce tables"
```

Details, including what autogenerate misses: [MIGRATIONS.md](MIGRATIONS.md).

## Shipping a change to servers

Servers only run the update pipeline when `VERSION` changes. CI fails a pull request that touches `lenny/`, `docker/`, `alembic/versions/`, `compose.yaml` or `requirements.txt` without bumping `VERSION`. New settings belong in `docker/configure.sh` in the `KEY="${KEY:-default}"` form, so `make update` can add them to existing installs.

On merge to `main`, `.github/workflows/docker-publish.yml` builds any image whose inputs changed, for amd64 and arm64, and publishes it to ghcr.io. Read [releasing-updates.md](plans/releasing-updates.md) before changing the update engine, and [update-engine.md](plans/update-engine.md) for how it works.

## Layout

```text
lenny/
  app.py            FastAPI app
  configs/          settings read from the env files
  core/             models, lending, auth, OAuth 2.0, Open Library, S3, importers
  routes/           HTTP routes
alembic/versions/   migrations
docker/
  api/ reader/ lenny-app/ opds-client/   image Dockerfiles
  nginx/            nginx config for lenny_api
  garage/           Garage config
  configure.sh      writes the env files
  utils/            lenny.sh, update engine, tunnel, doctor, image hashing
scripts/            CLI tools run inside lenny_api (addbook, preload, oauth2_client, librarian)
tests/              pytest suite
docs/               these manuals
```

## Contributing

1. Open or pick an [issue](https://github.com/ArchiveLabs/lenny/issues) and say you are working on it.
2. Branch from `main`, keep the change focused, add or update tests.
3. Run `pytest`. Bump `VERSION` if CI asks for it.
4. Open a pull request against `main` describing what changed and how you tested it.

Questions: ask on the issue, or [request an invite to the Lenny Slack channel](https://forms.gle/b4HDcWVRhT3fvqcQ6).
