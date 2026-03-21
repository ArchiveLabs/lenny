# Lenny Update Engine

## What It Is

A single command — `make update` — that brings any Lenny installation to the latest version. The engine pulls the latest code, syncs environment variables, backs up the database, rebuilds containers, and stamps the version. All changes ship in PRs — the code in the PR IS the migration.

## Bootstrapping (Existing Installations)

The first release that includes the update engine requires a one-time manual step:

```sh
git pull          # get the update engine code
make update       # engine takes over from here
```

This works because the engine handles the "no `.lenny-version` file" case:
- Installed version is set to `"unset"`
- `"unset" != "0.1.0"` → full pipeline runs
- Global pipeline syncs env, pulls images, rebuilds, restarts
- Alembic migrations run automatically on container startup
- `.lenny-version` is stamped to `0.1.0`

After this, all future updates are just `make update`. The engine runs `git pull` internally — users never need to do it manually again.

## How It Works

```
make update
    └── docker/utils/update.sh          (orchestrator)
            ├── 020_env_sync.sh         (always runs)
            ├── 030_pull_images.sh      (always runs)
            ├── 035_backup_db.sh        (always runs)
            └── 040_build_and_restart.sh (always runs)

VERSION              ← target version (committed to git, bumped on release)
.lenny-version       ← installed version (local only, gitignored)
```

The pipeline scripts live in `docker/utils/update/` and run on every update. They bring the environment into a consistent state regardless of what changed.

| Script | Purpose |
|---|---|
| `020_env_sync.sh` | Sync missing env vars into `.env` and `reader.env` from `configure.sh` defaults |
| `030_pull_images.sh` | `docker compose pull --ignore-buildable` for external images |
| `035_backup_db.sh` | `pg_dump` the database before migrations run |
| `040_build_and_restart.sh` | Rebuild custom images + `docker compose up -d` |

## The Four Steps

1. **Pre-flight** — Acquire update lock (prevents concurrent runs). Docker running? `.env` exists? Working tree clean? Compose command available?
2. **Git pull** — `git pull --ff-only` to get latest code. Compare `.lenny-version` (installed) to `VERSION` (target). If equal, exit early.
3. **Global pipeline** — Env sync, pull images, backup database, rebuild, restart. Alembic migrations run automatically on container startup via `migrate.sh`.
4. **Stamp** — Write target version to `.lenny-version`

If any step fails, the engine halts. `.lenny-version` is NOT updated. Re-running `make update` is always safe — every script is idempotent.

## How Changes Ship

Every change ships in a PR. No per-version migration scripts needed:

- **New env variable** → Add to `configure.sh` heredoc. `env_sync` picks it up automatically on next `make update`.
- **Schema change** → Create an Alembic migration. It runs automatically on container startup.
- **New Docker service** → Add to `compose.yaml`. The rebuild step starts it.
- **Code change** → It's in the PR. The rebuild bakes it into the image.
- **New dependency** → Update `requirements.txt` or Dockerfile. The rebuild picks it up.

The engine doesn't need to know what changed — it pulls, syncs, rebuilds, and restarts. The code in the PR handles the rest.

## Releasing a New Version

```bash
# 1. Bump the version
echo "0.2.0" > VERSION

# 2. Commit and push
git add VERSION
git commit -m "release: v0.2.0 — description"
git push
```

That's it. When users run `make update`, the engine detects the version difference and runs the full pipeline.

## Modifying the Global Pipeline

To add a new global step, drop a numbered `.sh` file in `docker/utils/update/`. Gaps in numbering (020, 030, 040) leave room for insertion (025, 035, etc.).

**Rules for pipeline scripts:**
- Must be idempotent — safe to re-run if interrupted
- Must use `#!/usr/bin/env bash` and `set -euo pipefail`
- Have access to: `$LENNY_ROOT`, `$LENNY_COMPOSE_PROJECT`, `$COMPOSE_CMD`, `$LENNY_INSTALLED_VERSION`, `$LENNY_TARGET_VERSION`
- Exit 0 on success, non-zero on failure (halts the engine)
- No interactive prompts — scripts run unattended
- No GNU-specific flags (macOS/Linux compatibility)

## Data Safety

The update engine takes a database backup **before** any rebuild or migration runs. This is handled by `035_backup_db.sh`.

**Why this matters:** When `040_build_and_restart.sh` runs `docker compose up -d`, the API container starts and `migrate.sh` executes `alembic upgrade head`. If a migration is destructive (drops columns, transforms data), the change is irreversible. The backup ensures you can always roll back.

**Backup location:** `backups/<db_name>_<timestamp>.sql` (gitignored). Only the 5 most recent backups are kept; older dumps are automatically rotated out.

**Backup integrity:** After each dump, the engine verifies the PostgreSQL dump footer (`PostgreSQL database dump complete`) is present. A missing footer warns about potential truncation from disk-full or interrupted writes.

**To restore from a backup:**
```bash
# Stop the API container to prevent connections
docker compose -p lenny stop api

# Restore the dump
docker exec -i lenny_db psql -U <DB_USER> -d <DB_NAME> < backups/<dump_file>.sql

# Restart
docker compose -p lenny up -d
```

**What's protected:**
- Database schema and data — `pg_dump` before any migration runs
- `.env` and `reader.env` — backed up before modification, only appends missing vars, never deletes or overwrites existing values, never removes user-added variables
- S3/MinIO object storage (books, files) — the `s3_data` Docker volume is never touched by the update engine
- All Docker volumes (`db_data`, `s3_data`, `readium_data`) — the engine never runs `docker compose down -v` or `docker volume rm`

**What the engine never does:**
- Never runs `docker compose down -v` (would destroy volumes)
- Never runs `docker volume rm` (would destroy data)
- Never deletes `.env` or `reader.env` (user's configuration)
- Never overwrites values in `.env` or `reader.env` (only appends new keys)
- Never force-merges git history (uses `--ff-only`)

**Backups created per update:**
- `backups/<db_name>_<timestamp>.sql` — database dump
- `backups/.env.<timestamp>.bak` — `.env` snapshot (only if env sync has changes to apply)
- `backups/reader.env.<timestamp>.bak` — `reader.env` snapshot (only if env sync has changes to apply)

## Cross-Platform Notes

- `#!/usr/bin/env bash` everywhere
- No GNU-specific flags (`sed -i` differs on macOS vs Linux)
- Compose command auto-detected: tries `docker compose` (v2) first, falls back to `docker-compose` (v1). All pipeline scripts use `$COMPOSE_CMD` instead of hardcoding either variant.
- Update lock uses `mkdir` (atomic on all POSIX systems) instead of `flock` for portability
