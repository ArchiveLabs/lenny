#!/usr/bin/env bash

./docker/configure.sh

echo "[+] Loading .env files"
export $(grep -v '^#' .env | xargs)
export $(grep -v '^#' reader.env | xargs)

source "$(dirname "$0")/docker_helpers.sh"
source "$(dirname "$0")/tunnel.sh"

LENNY_PROXY="${LENNY_PROXY:-$(get_tunnel)}"

if [[ "$1" == "--rebuild" ]]; then
    docker compose down --volumes --remove-orphans
    docker compose build --no-cache
fi

if [[ "$1" == "--start" || "$1" == "--rebuild" || "$1" == "--rebuild-reader" ]]; then
    docker compose -p lenny up -d
elif [[ "$1" == "--restart" ]]; then
    docker compose -p lenny restart api
elif [[ "$1" == "--stop" ]]; then
    docker compose -p lenny stop
else
    echo "Usage: $0 --start | --rebuild | --restart"
    exit 1
fi

if [[ "$1" == "--rebuild-reader" ]]; then
    CURRENT_PROXY=$(docker exec lenny_reader printenv LENNY_PROXY || echo "")
    if [[ "$CURRENT_PROXY" == "$LENNY_PROXY" ]]; then
        echo "[+] Reader already has correct proxy. Skipping rebuild."
    else
        PROXY_HOST="${LENNY_PROXY#https://}"
        echo "[+] LENNY_PROXY detected at $LENNY_PROXY"
        echo "[+] Updating NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS for reader and rebuilding"
        export NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS="${NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS:+$NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS,}$PROXY_HOST"
        docker compose -p lenny up -d --build reader

        if docker ps -q -f name=lenny_api >/dev/null; then
            echo "[+] Restarting lenny_api to pick up updated LENNY_PROXY"
            docker compose -p lenny up -d --no-deps api
        fi
    fi
fi