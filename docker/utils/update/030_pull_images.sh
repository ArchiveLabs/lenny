#!/usr/bin/env bash
set -euo pipefail

# Pull latest versions of external Docker images (postgres, garage, readium).
# Lenny's own images (api, reader, admin, opds) are skipped here via
# --ignore-buildable; step 040 resolves and pulls them by content-hash tag.

cd "$LENNY_ROOT"

# --ignore-buildable: skip api/reader/admin (we build those locally)
# --quiet: suppress progress output
echo "Checking for updated external images..."
if $COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" pull --ignore-buildable --quiet 2>&1; then
    echo "External images up to date."
else
    echo "Warning: image pull had issues (network may be down). Continuing with cached images."
fi
