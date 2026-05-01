#!/usr/bin/env bash

source "$(dirname "$0")/docker_helpers.sh"

PRELOAD="${1:-}"

if wait_for_docker_container "lenny_api" 15 2; then
    if [[ "$PRELOAD" =~ ^[0-9]+$ ]]; then
        EST_MIN=$(echo "scale=2; $PRELOAD * 10 / 60" | bc)
        LIMIT="-n $PRELOAD"
    else
        EST_MIN=$(echo "scale=2; 800 * 10 / 60" | bc)
        LIMIT=""
    fi
    echo "[+] Preloading ${PRELOAD:-ALL}/~800 book(s) from StandardEbooks (~$EST_MIN minutes)..."
    if docker exec -i lenny_api python scripts/preload.py $LIMIT; then
        echo "[✓] Completed preload"
    else
        echo "[✗] Preload failed — check logs above"
        exit 1
    fi
fi
