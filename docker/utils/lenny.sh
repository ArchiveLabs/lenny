#!/usr/bin/env bash

./docker/configure.sh

echo "[+] Loading .env files"
export $(grep -v '^#' .env | xargs)
export $(grep -v '^#' reader.env | xargs)

UTILS_DIR="$(dirname "$0")"
source "$UTILS_DIR/docker_helpers.sh"
source "$UTILS_DIR/tunnel.sh"
source "$UTILS_DIR/images.sh"

# Pin the compose project so container/network names are stable and the image
# helpers (docker/utils/images.sh) pull/build under the same project.
export LENNY_COMPOSE_PROJECT="lenny"
export COMPOSE_CMD="docker compose"
COMPOSE="docker compose -p lenny"

LENNY_PROXY="${LENNY_PROXY:-$(get_tunnel)}"

READER_WAS_RUNNING=$(docker ps -q -f name=lenny_reader 2>/dev/null)

case "$1" in
    --rebuild)
        # Dev path: deliberately builds every image locally. Wipes data.
        echo "[!] WARNING: --rebuild wipes the database volume and all data."
        lenny_export_image_tags
        $COMPOSE down --volumes --remove-orphans
        $COMPOSE build --no-cache
        $COMPOSE up -d --no-build
        ;;
    --build)
        # Dev path: deliberately builds every image locally. Wipes data.
        echo "[!] WARNING: --build wipes the database volume and all data."
        lenny_export_image_tags
        $COMPOSE down --volumes --remove-orphans
        $COMPOSE build
        $COMPOSE up -d --no-build
        ;;
    --start|--rebuild-reader)
        # Production path: pull prebaked images (api auto-builds on a miss; node
        # images fail loudly unless LENNY_ALLOW_LOCAL_BUILD=1), then start WITHOUT
        # building — so the expensive reader build can never be triggered silently.
        lenny_pull_images || exit 1
        $COMPOSE up -d --no-build
        ;;
    --restart)
        lenny_export_image_tags
        $COMPOSE up -d --no-build --force-recreate --no-deps api
        ;;
    --redeploy)
        # Rebuilds the (cheap) API image and applies migrations — data preserved.
        echo "[+] Redeploying API (data preserved)..."
        lenny_export_image_tags
        $COMPOSE up -d --build api
        echo "[+] Redeploy complete."
        ;;
    --stop)
        $COMPOSE stop
        ;;
    *)
        echo "Usage: $0 --start | --build | --rebuild | --redeploy | --restart | --stop"
        exit 1
        ;;
esac

# ── Tunnel proxy host (invoked by `make tunnel` via --rebuild-reader) ─────────
# The reader only opens manifests whose host is in MANIFEST_ALLOWED_DOMAINS,
# which it reads at RUNTIME from reader.env (thorium-web's verify-manifest route
# handler — no NEXT_PUBLIC_ prefix, so Next never inlines it). A changing tunnel
# host therefore needs an env append plus a reader RESTART, never a rebuild.
# (The previous version read NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS, a name nothing
# sets, so its skip test never matched and it rebuilt the reader every time.)
if [[ "$1" == "--rebuild-reader" ]] && [[ -n "$LENNY_PROXY" ]] && [[ -n "$READER_WAS_RUNNING" ]]; then
    echo "[+] LENNY_PROXY detected at $LENNY_PROXY"
    PROXY_HOST="${LENNY_PROXY#https://}"
    PROXY_HOST="${PROXY_HOST#http://}"
    PROXY_HOST="${PROXY_HOST%%/*}"
    READER_ENV="reader.env"

    CURRENT=$(grep -E '^MANIFEST_ALLOWED_DOMAINS=' "$READER_ENV" | head -1 | cut -d= -f2-)
    if [[ ",$CURRENT," == *",$PROXY_HOST,"* ]]; then
        echo "[+] Reader already allows $PROXY_HOST. No restart needed."
    else
        echo "[+] Adding $PROXY_HOST to MANIFEST_ALLOWED_DOMAINS in reader.env and restarting reader"
        if grep -qE '^MANIFEST_ALLOWED_DOMAINS=' "$READER_ENV"; then
            sed -i.bak -E "s|^(MANIFEST_ALLOWED_DOMAINS=.*)\$|\1,$PROXY_HOST|" "$READER_ENV" && rm -f "$READER_ENV.bak"
        else
            echo "MANIFEST_ALLOWED_DOMAINS=$PROXY_HOST" >> "$READER_ENV"
        fi
        # Recreate (not rebuild) so the container re-reads reader.env.
        lenny_export_image_tags
        $COMPOSE up -d --no-build --force-recreate --no-deps reader

        if docker ps -q -f name=lenny_api >/dev/null; then
            echo "[+] Restarting lenny_api to pick up updated LENNY_PROXY"
            $COMPOSE up -d --no-build --no-deps api
        fi
    fi
fi
