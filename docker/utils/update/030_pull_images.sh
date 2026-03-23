#!/usr/bin/env bash
set -euo pipefail

# Pull latest versions of external Docker images (postgres, minio, readium).
# Custom-built images (api, reader) are skipped — they're rebuilt in the next step.

cd "$LENNY_ROOT"

# --ignore-buildable: skip api/reader (we build those locally)
# --quiet: suppress progress output
echo "Checking for updated external images..."
if $COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" pull --ignore-buildable --quiet 2>&1; then
    echo "External images up to date."
else
    echo "Warning: image pull had issues (network may be down). Continuing with cached images."
fi
