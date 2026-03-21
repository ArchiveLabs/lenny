# Releasing Updates — Developer Guide

This is for developers building and shipping Lenny updates. It covers what you need to know, what you need to do, and what will bite you if you're not careful.

For how the engine works internally, see [update-engine.md](update-engine.md).

## The Mental Model

Every change ships in a PR. The update engine doesn't know or care what changed — it pulls, syncs, rebuilds, and restarts. Your job is to make sure the PR contains everything needed for the change to take effect.

```
make update
    └── update.sh (orchestrator)
            ├── 020_env_sync.sh          ← syncs new env vars
            ├── 030_pull_images.sh       ← pulls base images
            ├── 035_backup_db.sh         ← backs up database
            └── 040_build_and_restart.sh ← rebuilds & restarts
                                            └── migrate.sh runs on startup
                                                └── alembic upgrade head
```

**One question to ask yourself:** "Does my PR contain everything needed for this change to work after `make update`?"

If yes, you're done. The engine handles the rest.

## Releasing a New Version — Checklist

### 1. Bump `VERSION`

```bash
echo "0.2.0" > VERSION
```

This is the trigger. When a user runs `make update`, the engine compares their `.lenny-version` to this file. If they differ, the update runs.

### 2. Commit and push

```bash
git add VERSION
git commit -m "release: v0.2.0 — description of what changed"
git push
```

That's it. Users run `make update` and the engine does the rest.

## How Different Changes Ship

### Adding a new env variable

Add the variable to `configure.sh`'s heredoc template. The global pipeline's `020_env_sync.sh` handles syncing it automatically — no migration script needed.

```bash
# In configure.sh, add the variable assignment:
MY_NEW_VAR="${MY_NEW_VAR:-default_value}"

# And add it to the heredoc:
MY_NEW_VAR=$MY_NEW_VAR
```

On next `make update`, env_sync detects the missing variable and appends it with the default value.

### Adding a database migration

Create an Alembic migration. It runs automatically on container startup:

```bash
# Generate the migration
make migration msg="add full text search"

# The migration file is created in alembic/versions/
# Edit it, commit it, push it
```

No update engine changes needed — `migrate.sh` runs `alembic upgrade head` every time the container starts.

### Adding a new Docker service

Add the service to `compose.yaml`. The global pipeline's `040_build_and_restart.sh` will start it automatically on next update.

### Changing application code

Just commit and push. The rebuild step bakes the new code into the Docker image.

## Rules for Pipeline Scripts

If you need to add a new step to the global pipeline (rare), drop a numbered `.sh` file in `docker/utils/update/`.

### Every script MUST be idempotent

This is the most important rule. If the engine fails halfway through and the user re-runs `make update`, your script runs again. It must not break.

**Bad:**
```bash
# Creates a duplicate entry on re-run
echo "NEW_VAR=value" >> "$LENNY_ROOT/.env"
```

**Good:**
```bash
# Only adds if missing
if ! grep -q '^NEW_VAR=' "$LENNY_ROOT/.env"; then
    echo "NEW_VAR=value" >> "$LENNY_ROOT/.env"
fi
```

### Every script MUST use strict mode

```bash
#!/usr/bin/env bash
set -euo pipefail
```

### Every script has these variables available

| Variable | Value | Example |
|---|---|---|
| `$LENNY_ROOT` | Repo root (absolute path) | `/home/user/lenny` |
| `$LENNY_COMPOSE_PROJECT` | Docker compose project name | `lenny` |
| `$COMPOSE_CMD` | Detected compose command | `docker compose` or `docker-compose` |
| `$LENNY_INSTALLED_VERSION` | Version before this update | `0.1.0` or `unset` |
| `$LENNY_TARGET_VERSION` | Version we're updating to | `0.2.0` |

### No interactive prompts

Scripts run unattended. No `read`, no `select`, no "press enter to continue".

### No GNU-specific flags

macOS and Linux behave differently:
- `sed -i` needs `''` on macOS, nothing on Linux — use `sed ... > tmp && mv tmp original` instead
- `grep -P` (PCRE) doesn't exist on macOS — use `grep -E` (extended regex)

## Things That Will Bite You

### `.lenny-version` only updates on full success

If any pipeline step fails, the version is not stamped. On re-run, the full pipeline runs again. This is why idempotency matters.

### Backups rotate — only 5 are kept

The engine keeps the 5 most recent `.sql` dumps. Don't rely on old backups being around.

### The orchestrator is off-limits

`docker/utils/update.sh` is the orchestrator. It handles lock acquisition, pre-flight checks, version detection, script discovery, and version stamping. You should never need to edit it.

## Testing Your Update

Before pushing:

1. **Test fresh install:** Remove `.lenny-version`, run `make update` — everything should work from scratch
2. **Test idempotency:** Run `make update` twice in a row — second run should be a no-op ("Already up to date")
3. **Test re-run after failure:** Simulate a failure mid-pipeline, then re-run — should recover cleanly
4. **Run `make doctor`:** Verify the environment is healthy after update

## Quick Reference

| I want to... | Do this |
|---|---|
| Release a new version | Bump `VERSION`, commit, push |
| Add a new env var | Add to `configure.sh` heredoc (env sync handles the rest) |
| Add a DB migration | `make migration msg="description"` |
| Add a global pipeline step | Numbered `.sh` in `docker/utils/update/` |
| Check environment health | `make doctor` |
| Debug a failed update | Read the error, fix the issue, re-run `make update` |
