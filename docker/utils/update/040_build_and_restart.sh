#!/usr/bin/env bash
set -euo pipefail

# Rebuild custom images and restart containers
# Alembic migrations run automatically on API container startup via migrate.sh

cd "$LENNY_ROOT"

# Build API (critical — must succeed)
echo "Building API image..."
$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" build --pull api

# Build reader (non-critical — warn but don't fail the update)
echo "Building reader image..."
if ! $COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" build --pull reader; then
    echo ""
    echo "WARNING: Reader build failed. The API will still start."
    echo "The reader may use a cached image or be unavailable."
    echo "To retry the reader build later: $COMPOSE_CMD -p $LENNY_COMPOSE_PROJECT build --no-cache reader"
    echo ""
fi

# Restart all services
$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" up -d
