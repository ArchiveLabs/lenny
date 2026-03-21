#!/usr/bin/env bash
set -euo pipefail

# Pull latest versions of external Docker images (postgres, minio, readium)

$COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" -f "$LENNY_ROOT/compose.yaml" pull --ignore-buildable
