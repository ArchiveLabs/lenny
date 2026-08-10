#!/usr/bin/env bash
set -euo pipefail

# One-time migration of existing MinIO 'bookshelf' bucket contents to Garage,
# for installs upgrading from a pre-Garage version. No-ops on fresh installs
# (no old MinIO volume exists) and on any re-run after a completed migration.
#
# Safety:
# - Never deletes the old s3_data volume or MinIO container. If the container
#   is still running, it's stopped (not removed) before reading, to avoid
#   racing step 4's `up -d s3`, which recreates it as the Garage service the
#   moment it runs regardless of whether we've read from it yet. The
#   container object is left in place for `make cleanup-old-s3` once the
#   admin has verified the migrated Garage bucket.
# - Idempotent: mc mirror only copies missing/changed objects, so a retry
#   after a failure (network blip, disk full, etc.) resumes safely.
# - Never writes the completion marker until the mirror step exits 0.

ENV_FILE="$LENNY_ROOT/.env"
MARKER="S3_GARAGE_MIGRATED"
NETWORK="${LENNY_COMPOSE_PROJECT}_lenny_network"
OLD_VOLUME="${LENNY_COMPOSE_PROJECT}_s3_data"
OLD_CONTAINER="lenny_s3"
TEMP_SOURCE_CONTAINER="lenny_s3_migration_source"
# MinIO rejects any request whose Host header contains an underscore
# (returns 400 InvalidRequest "invalid hostname") — container/service names
# with underscores are otherwise valid Docker DNS names, so mc needs a
# hyphenated alias to actually reach it over S3.
TEMP_SOURCE_ALIAS="lenny-s3-migration-source"

genhex() {
    bytes=${1:-32}
    dd if=/dev/urandom bs=1 count="$bytes" 2>/dev/null | od -An -tx1 | tr -d ' \n'
}

genpass() {
    len=${1:-32}
    dd if=/dev/urandom bs=1 count=$((len * 2)) 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | head -c "$len"
}

env_get() {
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-
}

env_set_if_missing() {
    grep -qE "^$1=" "$ENV_FILE" 2>/dev/null || echo "$1=$2" >> "$ENV_FILE"
}

# ── 0. Backfill Garage-only secrets into .env (additive only, idempotent) ───
# Not handled by 020_env_sync.sh's generic $(genpass N) sync — hex secrets
# and the region default need their own generation logic.
env_set_if_missing S3_REGION "garage"
env_set_if_missing S3_RPC_SECRET "$(genhex 32)"
env_set_if_missing S3_ADMIN_TOKEN "$(genpass 32)"
if grep -qE '^S3_PROVIDER=' "$ENV_FILE"; then
    sed -i.bak 's/^S3_PROVIDER=.*/S3_PROVIDER=garage/' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
else
    echo "S3_PROVIDER=garage" >> "$ENV_FILE"
fi

# ── 1. Already migrated? ─────────────────────────────────────────────────
if [ "$(env_get "$MARKER")" = "true" ]; then
    echo "S3 data already migrated to Garage (${MARKER}=true). Skipping."
    exit 0
fi

# ── 2. Fresh install? (no old MinIO volume at all) ───────────────────────
if ! docker volume inspect "$OLD_VOLUME" >/dev/null 2>&1; then
    echo "No old MinIO volume ($OLD_VOLUME) found — fresh install, nothing to migrate."
    echo "${MARKER}=true" >> "$ENV_FILE"
    exit 0
fi

echo "Old MinIO volume found — migrating book files to Garage..."

# ── 3. Read from a temp instance off the old volume, not the live container. ──
# Step 4's `up -d s3` recreates the "s3" compose service in place (same
# project + service key, new image/container_name) the moment it runs,
# regardless of whether the old container is up — so reusing it as the
# mirror source would race that teardown. Stop it first (data untouched)
# and always read through our own temp container instead.
docker stop "$OLD_CONTAINER" >/dev/null 2>&1 || true

docker rm -f "$TEMP_SOURCE_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$TEMP_SOURCE_CONTAINER" \
    --network "$NETWORK" \
    --network-alias "$TEMP_SOURCE_ALIAS" \
    -v "$OLD_VOLUME:/data" \
    -e MINIO_ROOT_USER="$(env_get S3_ACCESS_KEY)" \
    -e MINIO_ROOT_PASSWORD="$(env_get S3_SECRET_KEY)" \
    minio/minio:latest server /data >/dev/null
SOURCE_URL="http://${TEMP_SOURCE_ALIAS}:9000"

cleanup() {
    docker rm -f "$TEMP_SOURCE_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# ── 4. Bring up the new Garage service (compose-managed, from the already- ──
#      updated compose.yaml) and wait for it to report healthy.
$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" -f "$LENNY_ROOT/compose.yaml" up -d s3

echo "Waiting for Garage to become healthy..."
status="starting"
for _ in $(seq 1 30); do
    status=$(docker inspect -f '{{.State.Health.Status}}' lenny_object_store 2>/dev/null || echo "starting")
    [ "$status" = "healthy" ] && break
    sleep 2
done
if [ "$status" != "healthy" ]; then
    echo "Garage did not become healthy in time." >&2
    exit 1
fi

# ── 5. Mirror the old bucket into the new one via a throwaway mc container. ──
# mc mirror (not `aws s3 sync`, which can't target two different endpoints
# in one invocation) copies cross-endpoint and auto-discovers Garage's
# signing region via GetBucketLocation, so no manual region flag is needed.
S3_ACCESS_KEY="$(env_get S3_ACCESS_KEY)"
S3_SECRET_KEY="$(env_get S3_SECRET_KEY)"

docker run --rm \
    --network "$NETWORK" \
    --entrypoint /bin/sh \
    minio/mc:latest -c "
        set -e
        mc alias set old '$SOURCE_URL' '$S3_ACCESS_KEY' '$S3_SECRET_KEY'
        mc alias set new 'http://s3:3900' '$S3_ACCESS_KEY' '$S3_SECRET_KEY'
        mc mirror --overwrite old/bookshelf new/bookshelf
    "

# ── 6. Mark complete ───────────────────────────────────────────────────────
echo "${MARKER}=true" >> "$ENV_FILE"
echo "Migration to Garage complete."
echo "Old MinIO data remains untouched in the '$OLD_VOLUME' volume and (if"
echo "still running) the '$OLD_CONTAINER' container. Once you've verified the"
echo "new bucket, remove them with: make cleanup-old-s3"
