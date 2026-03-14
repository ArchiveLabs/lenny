#!/usr/bin/env bash

./docker/configure.sh

echo "[+] Loading .env files"
export $(grep -v '^#' .env | xargs)
export $(grep -v '^#' reader.env | xargs)

source "$(dirname "$0")/docker_helpers.sh"
source "$(dirname "$0")/tunnel.sh"

LENNY_PROXY="${LENNY_PROXY:-$(get_tunnel)}"

READER_WAS_RUNNING=$(docker ps -q -f name=lenny_reader 2>/dev/null)

if [[ "$1" == "--rebuild" ]]; then
    echo "[!] WARNING: --rebuild wipes the database volume and all data."
    docker compose down --volumes --remove-orphans
    docker compose build --no-cache
fi

if [[ "$1" == "--build" ]]; then
    echo "[!] WARNING: --build wipes the database volume and all data."
    docker compose down --volumes --remove-orphans
    docker compose build
fi

if [[ "$1" == "--start" || "$1" == "--rebuild" || "$1" == "--build" || "$1" == "--rebuild-reader" ]]; then
    docker compose -p lenny up -d
elif [[ "$1" == "--restart" ]]; then
    docker compose -p lenny up -d --force-recreate --no-deps api
elif [[ "$1" == "--redeploy" ]]; then
    # Rebuilds API image and applies migrations — database and data are preserved.
    echo "[+] Redeploying API (data preserved)..."
    docker compose -p lenny up -d --build api
    echo "[+] Redeploy complete."
elif [[ "$1" == "--stop" ]]; then
    docker compose -p lenny stop
else
    echo "Usage: $0 --start | --build | --rebuild | --redeploy | --restart | --stop"
    exit 1
fi

if [[ "$1" == "--rebuild-reader" ]] && [[ -n "$LENNY_PROXY" ]] && [[ -n "$READER_WAS_RUNNING" ]]; then
    echo "[+] LENNY_PROXY detected at $LENNY_PROXY"
    ALLOWED_HOSTS=$(docker exec lenny_reader printenv NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS || echo "")
    PROXY_HOST="${LENNY_PROXY#https://}"
    if [[ "$ALLOWED_HOSTS" == *"$PROXY_HOST"* ]]; then
        echo "[+] Reader already has correct proxy. Skipping rebuild."
    else
        echo "[+] Updating NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS for reader and rebuilding"
        export NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS="${NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS:+$NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS,}$PROXY_HOST"
        docker compose -p lenny up -d --build reader

        if docker ps -q -f name=lenny_api >/dev/null; then
            echo "[+] Restarting lenny_api to pick up updated LENNY_PROXY"
            docker compose -p lenny up -d --no-deps api
        fi
    fi
fi
