# Dev/Prod Mode via LENNY_PRODUCTION

**Goal:** Production is the default mode. Devs opt-in to hot-reload by setting `LENNY_PRODUCTION=false` in `.env`.

**Architecture:** Single env var `LENNY_PRODUCTION` (true/false) controls whether uvicorn hot-reloads on code changes. Replaces the old broken `LENNY_RELOAD` variable. No migration scripts — `env_sync` adds the new variable automatically during `make update`.

## The Change

| What | Before | After |
|---|---|---|
| Env var | `LENNY_RELOAD=1` (broken, `0` still reloaded) | `LENNY_PRODUCTION=true` / `false` |
| New install default | dev mode | production (`LENNY_PRODUCTION=true`) |
| Python config | Read `LENNY_DEBUG` for reload | Read `LENNY_PRODUCTION` |
| Existing users | `make update` → env_sync adds `LENNY_PRODUCTION=true`, old `LENNY_RELOAD` ignored |

## Files Modified

| File | Change |
|---|---|
| `docker/api/Dockerfile:35` | CMD checks `LENNY_PRODUCTION=false` to add `--reload` |
| `docker/configure.sh:22,57` | Default `LENNY_PRODUCTION=true`, write to `.env` |
| `lenny/configs/__init__.py:42` | `'reload': LENNY_PRODUCTION == 'false'` |
| `docker/utils/update/020_env_sync.sh` | Fix: resolve literal defaults from configure.sh instead of stripping to empty |
