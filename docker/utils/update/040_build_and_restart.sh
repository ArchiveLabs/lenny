#!/usr/bin/env bash
set -euo pipefail

# Rebuild custom images and restart containers
# Alembic migrations run automatically on API container startup via migrate.sh

cd "$LENNY_ROOT"

# Build and restart
$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" build api reader
$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" up -d
