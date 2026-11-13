# Troubleshooting

Start with:

```sh
make doctor                  # Docker, .env, database, disk, version
docker ps --filter name=lenny
make log                     # follow all logs; Ctrl+C to stop
docker logs -f lenny_api     # one service
```

`make log` and `make teardown` call `docker compose` without `-p lenny`, so they only find the stack when the checkout directory is named `lenny`. From another directory use `docker compose -p lenny logs -f`.

## `make start` or `make update` stops with "no prebaked image"

No published image matches your code for the reader, admin UI or catalog. Common causes: you set `NEXT_PUBLIC_API_URL` (it is compiled into the admin image), you are on an unmerged branch, or CI has not finished publishing a just-merged change. Wait for CI, or build on this host:

```sh
LENNY_ALLOW_LOCAL_BUILD=1 make update
```

The reader build needs a lot of memory; it has been killed on 1 GB hosts. A `401` or `denied` in the message means the host cannot read `ghcr.io/archivelabs`; check your network or run `docker login ghcr.io`.

## `make update` says "Working tree has uncommitted changes"

The update needs a clean git checkout, and untracked files count. Run `git status`. Move book files and other local files out of the checkout; commit or discard edits to tracked files. Your env files and `backups/` are ignored by git and do not block it.

## `make update` says "Already up to date" but something is broken

The update engine only runs when `VERSION` changes. To restart everything with the current code and images, run `make start`.

## `make update` or `make doctor` fails with "not a git repository"

The directory was created by `install.sh`, which downloads a tarball. Move to a git checkout: clone into a new directory, copy `.env`, `auth.env`, `ol.env`, `reader.env` and `loan.env` across, and run `make start` there. The containers and volumes belong to the compose project `lenny`, so data carries over.

## Port already in use

- `8080`: set `LENNY_PORT` in `.env` and run `make start`. (`make tunnel` still points at 8080.)
- `127.0.0.1:5432`: another Postgres is running on the host. Stop it. Changing `DB_PORT` does not help: the API uses the same value to reach the database inside Docker, and Postgres there listens on 5432.
- `127.0.0.1:9000`: something else uses port 9000 on loopback. Garage's port is fixed in `compose.yaml`.

## Database connection errors

Check that the database container is up and healthy:

```sh
docker ps --filter name=lenny_db
make start
```

`make restart` only recreates the API container, so it does not bring a stopped database back; use `make start`. If the database is damaged beyond that, restore a dump from `backups/` ([Back up and restore](install.md#back-up-and-restore)).

`make resetdb` is a last resort: it deletes the database **and all stored books**.

## Books will not open in the reader

- The reader only opens books from hosts in `MANIFEST_ALLOWED_DOMAINS` in `reader.env`. If you use your own domain, add it and run `make start`.
- `LENNY_PROXY` in `.env` must be the address patrons use. A stale tunnel URL there breaks links; run `make untunnel` or correct it, then `make start`.
- Encrypted books need an active loan; borrowing needs a lending mode other than `none` ([Lending modes](admin.md#lending-modes)).

## Patrons cannot borrow

- Lending mode `none`: nobody can borrow. Set `ol` or `external`.
- Mode `ol` without keys: run `make ol-login` or log in under **Settings → Open Library**.
- Sign-in comes back to the wrong address: set `LENNY_PROXY` to the public URL. A browser OPDS client on another origin also needs its host in `LENNY_OPDS_ALLOWED_HOSTS`.
- OIDC problems: see the table at the end of [OAUTH.md](OAUTH.md).

## The tunnel stopped working

After a network drop or sleep:

```sh
make untunnel tunnel
```

The quick-tunnel hostname changes each time. Readers and apps using the old URL need the new one (`make url`).

## `make tunnel` fails on arm64 Linux

`docker/utils/tunnel.sh` downloads the Linux amd64 build of `cloudflared`, which will not run on arm64. Install `cloudflared` for your architecture from [Cloudflare's downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/) so it is on your `PATH`, then run `make tunnel` again; the script skips the download when `cloudflared` exists. On macOS it uses Homebrew and works on both architectures.

## `make addbook` says "File not found" or "Cannot read file"

Check the path. On macOS, files in `~/Downloads`, `~/Desktop` or `~/Documents` may be blocked for the terminal; move the file elsewhere or give the terminal Full Disk Access.

## Admin password lost

```sh
make admin-passwd USERNAME=admin
```

Editing `ADMIN_PASSWORD` in `auth.env` has no effect after the first start.

## Disk is filling up

```sh
docker system df
make prune
```

`make prune` removes dangling images and caps the build cache at 2 GB. It never touches volumes. Database dumps in `backups/` are capped at the five newest.

## Start over completely

This deletes the database and every stored book:

```sh
make stop
make rebuild
```

`make rebuild` builds every image locally without cache. To start clean with prebaked images instead: `make resetdb`, then `make start`.

## Still stuck

Open an [issue](https://github.com/ArchiveLabs/lenny/issues) with the output of `make doctor`, your `VERSION`, your OS and CPU architecture, and the relevant log lines. Remove secrets from anything you paste.
