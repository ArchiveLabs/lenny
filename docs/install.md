# Install and operate Lenny

- [Requirements](#requirements)
- [Install](#install)
- [What is running](#what-is-running)
- [Update](#update)
- [Back up and restore](#back-up-and-restore)
- [Public access and HTTPS](#public-access-and-https)
- [Start, stop and reset](#start-stop-and-reset)

## Requirements

| | |
|---|---|
| OS | Linux or macOS. Windows is not supported. |
| CPU | amd64 (x86-64) or arm64 (Apple silicon, 64-bit Raspberry Pi OS). 32-bit ARM has no prebaked images. |
| Docker | Docker Engine or Docker Desktop with Compose v2 (`docker compose`). |
| Tools | `git`, `make`, `bash`, `curl`. `bc` for `make preload`. |
| Disk | `make doctor` warns below 5 GB free. Books, the database and images all live on this disk. |
| Ports | `8080` (or `LENNY_PORT`) for the web server. `127.0.0.1:5432` (Postgres) and `127.0.0.1:9000` (Garage S3) must be free on loopback. |

Memory: the prebaked images avoid the expensive builds on your server. Building the reader image yourself (see [Prebaked images](#prebaked-images)) has run out of memory on 1 GB hosts. On small machines set `LENNY_WORKERS=1` (see [Configuration](configuration.md#memory)).

## Install

Use a git checkout. `make update` and the image tagging both depend on git.

```sh
git clone https://github.com/ArchiveLabs/lenny.git
cd lenny
make start
```

`make start` does three things:

1. Runs `docker/configure.sh` (also `make configure`), which writes `.env`, `reader.env`, `auth.env`, `ol.env` and `loan.env` with fresh random secrets. Existing files are never overwritten.
2. Installs a `lenny` command in `~/.local/bin` that runs `make` in this directory, so `lenny start`, `lenny update` and so on work from anywhere.
3. Pulls Lenny's prebaked images from `ghcr.io/archivelabs` and starts the stack.

Then sign in at http://localhost:8080/admin as `admin`, with the password stored in `auth.env`:

```sh
grep ^ADMIN_PASSWORD= auth.env
```

That password is used once, to create the first admin account. After the first start, change it in the admin UI under **My account**. The [Admin guide](admin.md) covers the rest.

> [!IMPORTANT]
> `.env` holds `LENNY_SEED`. It signs sessions and keys the hash that identifies patrons. Back it up with the database and never change it: a restored database with a different seed loses every patron's loans.

### The install script

The website points to a one-line installer:

```sh
curl -fsSL https://raw.githubusercontent.com/ArchiveLabs/lenny/refs/heads/main/install.sh | sudo bash
```

It installs Docker if missing, asks whether to preload books and enable lending, and builds Lenny. Know what it does before you use it on a server:

- It downloads a tarball of `main`, not a git checkout. `make update` and `make doctor` do not work in that directory. To move to a checkout, clone into a new directory, copy the five env files across, and run `make start` there. The containers and volumes are named after the project `lenny`, not the directory, so your data carries over.
- It opens a public Cloudflare quick tunnel (`make tunnel`).
- It builds every image locally (`make rebuild`) instead of pulling the prebaked ones. This needs more memory and time.

Non-interactive use: pass `-y` (or `LENNY_DEFAULTS=1`) to answer "no" to every prompt, and set `LENNY_PRELOAD=1`, `LENNY_LENDING=1` or `LENNY_INDEXED=1` to say yes to one.

## What is running

`make start` runs seven containers in the compose project `lenny`:

| Container | Image | Role |
|---|---|---|
| `lenny_api` | `ghcr.io/archivelabs/lenny-api` | nginx on port 80 (published as `LENNY_PORT`) in front of the FastAPI app on 1337. The only way in from outside. |
| `lenny_db` | `postgres:16` | Database. Published on `127.0.0.1:${DB_PORT}` only. Tuned to the host's RAM at start. |
| `lenny_object_store` | `dxflrs/garage:v2.3.0` | Garage S3 storage for the EPUB files, bucket `bookshelf`. Published on `127.0.0.1:9000` only. |
| `lenny_readium` | `ghcr.io/readium/readium:0.10.1` | Serves book manifests and resources to the reader. Not published: nginx reaches it after the loan check. |
| `lenny_reader` | `ghcr.io/archivelabs/lenny-reader` | Thorium Web, at `/read`. Not published. |
| `lenny_admin` | `ghcr.io/archivelabs/lenny-admin` | The admin UI ([lenny-app](https://github.com/ArchiveLabs/lenny-app)), at `/admin`. |
| `lenny_opds` | `ghcr.io/archivelabs/lenny-opds` | The patron catalog ([opds-client](https://github.com/ArchiveLabs/opds-client)), at `/`. |

Data lives in the named volumes `lenny_db_data`, `lenny_garage_meta`, `lenny_garage_data` and `lenny_readium_data`.

## Update

```sh
make update
```

The update engine (`docker/utils/update.sh`):

1. Checks that Docker is running, `.env` exists, and the git working tree is clean. Untracked files count, so keep your EPUBs outside the checkout.
2. Pulls `main` with `git pull --ff-only` from the `upstream` remote if there is one, else `origin`. Stops here if `VERSION` matches the installed version in `.lenny-version`.
3. Adds any new settings to your env files, without changing existing values. Each changed file is copied to `backups/` first.
4. Pulls new third-party images and the prebaked Lenny images.
5. Dumps the database to `backups/<db_name>_<timestamp>.sql` and keeps the five newest dumps.
6. Restarts the stack. Database migrations run when the API container starts.

Every step can be re-run. If one fails, fix the cause and run `make update` again. Installs made before the update engine existed need one `git pull` first.

`make update` exits early when you are already on the latest `VERSION`. To restart after editing an env file, use `make start` (all services) or `make restart` (API only).

### Prebaked images

CI builds the four Lenny images (`lenny-api`, `lenny-reader`, `lenny-admin`, `lenny-opds`) for amd64 and arm64 and publishes them to `ghcr.io/archivelabs`. arm64 images exist from 0.9.3. Each image is tagged with a hash of its build inputs (`docker/utils/image-hash.sh`), so a server pulls exactly the image that matches its code.

If no image matches:

- `lenny-api` is built locally. It is small and changes with every release.
- `lenny-reader`, `lenny-admin` and `lenny-opds` are not built automatically. The command stops before touching any container and prints why. To build them on your server on purpose:

  ```sh
  LENNY_ALLOW_LOCAL_BUILD=1 make update    # or: LENNY_ALLOW_LOCAL_BUILD=1 make start
  ```

You will hit this if you set `NEXT_PUBLIC_API_URL`, if you run an unmerged branch, or right after a merge while CI is still publishing.

### After upgrading from MinIO

Installs older than the move to Garage have their old MinIO bucket copied into Garage by `make update`. The old container, volume and images stay until you remove them. Once you have checked that your books are all there:

```sh
make cleanup-old-s3
```

## Back up and restore

`make update` backs up the database only. A full backup has three parts:

| Part | Where |
|---|---|
| Config and secrets | `.env`, `auth.env`, `ol.env`, `reader.env`, `loan.env` in the checkout |
| Database | volume `lenny_db_data` |
| Book files | volumes `lenny_garage_meta` and `lenny_garage_data` |

Database dump while running:

```sh
docker exec lenny_db pg_dump -U "$(grep ^DB_USER= .env | cut -d= -f2)" -d "$(grep ^DB_NAME= .env | cut -d= -f2)" --clean --if-exists > backups/manual.sql
```

Book storage, with the stack stopped so the copy is consistent:

```sh
make stop
for v in garage_meta garage_data; do
  docker run --rm -v lenny_$v:/data -v "$PWD/backups":/backup alpine tar czf /backup/$v.tar.gz -C /data .
done
make start
```

Restore a database dump:

```sh
docker compose -p lenny stop api
docker exec -i lenny_db psql -U "$(grep ^DB_USER= .env | cut -d= -f2)" -d "$(grep ^DB_NAME= .env | cut -d= -f2)" < backups/<dump>.sql
make start
```

Restore book storage into the (stopped) volumes with the reverse `tar xzf`. Restore the env files before starting, and keep the original `LENNY_SEED`.

## Public access and HTTPS

Lenny's nginx serves plain HTTP on `LENNY_PORT`. Put HTTPS in front of it in one of two ways.

### Quick tunnel (testing)

```sh
make tunnel     # start a Cloudflare quick tunnel, then start Lenny
make url        # print the public catalog and OPDS URLs
make untunnel   # close it (make stop also closes it)
```

`make tunnel` installs `cloudflared` if missing, opens a `*.trycloudflare.com` address, saves it as `LENNY_PROXY` in `.env` and starts the stack. The hostname changes every time the tunnel restarts, so this is for trials, not for a library's public address.

Limits: the tunnel always points at port 8080, even if you changed `LENNY_PORT`. On Linux the script downloads the amd64 build of `cloudflared`; on arm64 install `cloudflared` yourself first (see [Troubleshooting](troubleshooting.md#make-tunnel-fails-on-arm64-linux)).

### Your own domain

Run a TLS reverse proxy (Caddy, nginx, Traefik, a load balancer) that forwards to `http://127.0.0.1:8080`, then tell Lenny its public address:

1. In `.env`, set `LENNY_PROXY=https://library.example.org` (no trailing slash). Lenny uses it in OPDS links, sign-in redirects and the OAuth metadata.
2. In `reader.env`, add your hostname to `MANIFEST_ALLOWED_DOMAINS`, for example `MANIFEST_ALLOWED_DOMAINS=127.0.0.1,localhost,*.trycloudflare.com,library.example.org`.
3. Run `make start` so the API and reader pick up the changes.

Forward the `Host` header and set `X-Forwarded-For` and `X-Forwarded-Proto`. Lenny ties sessions and one-time passwords to the patron's address from `X-Forwarded-For`; which proxies it trusts is set by `LENNY_FORWARDED_ALLOW_IPS` (see [Configuration](configuration.md#env)).

`LENNY_SSL_CRT` and `LENNY_SSL_KEY` in `.env` are not used by the Docker setup. Terminate TLS in front of Lenny.

## Start, stop and reset

| Command | Effect | Data |
|---|---|---|
| `make start` | Pull images, start everything | kept |
| `make restart` | Recreate the API container | kept |
| `make stop` | Stop all containers and close the tunnel | kept |
| `make redeploy` | Rebuild the API image from your checkout and restart it | kept |
| `make resetdb` | `docker compose down -v` | **database and books deleted** |
| `make teardown` | Remove containers, volumes and orphans | **database and books deleted** |
| `make build`, `make rebuild` | Delete volumes, build every image locally, start | **database and books deleted** |

The full list of targets is in [Configuration](configuration.md#make-targets).
