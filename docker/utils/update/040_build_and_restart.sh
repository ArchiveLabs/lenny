#!/usr/bin/env bash
set -euo pipefail

# Resolve Lenny's prebaked images and restart the stack.
# Alembic migrations run automatically on API container startup via migrate.sh.
#
# Previously this step BUILT api, reader and admin locally on every update. The
# reader build (a full thorium-web `pnpm build`) is the single most expensive
# thing in the stack, and on a small VPS it has been OOM-killed — which, because
# nginx refuses to start when a proxy_pass upstream is unresolvable, took the
# whole API down. CI now prebakes these images; we pull them instead.

cd "$LENNY_ROOT"

# lenny_pull_images (docker/utils/images.sh):
#   - pulls each image by the content-hash tag that matches the current tree
#   - auto-builds only the cheap api image on a miss
#   - FAILS LOUDLY on a missing node image (reader/admin/opds) unless
#     LENNY_ALLOW_LOCAL_BUILD=1, without starting a partial stack
# COMPOSE_CMD and LENNY_COMPOSE_PROJECT are exported by update.sh.
source "$LENNY_ROOT/docker/utils/images.sh"

echo "Resolving prebaked images..."
lenny_pull_images

# Start without building: every image is already present (pulled, or the cheap
# api built above), so this can never trigger a surprise reader build.
echo "Starting stack..."
$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" up -d --no-build
