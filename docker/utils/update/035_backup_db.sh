#!/usr/bin/env bash
set -euo pipefail

# Back up the database before rebuild/restart (which triggers Alembic migrations).
# Dump is saved to $LENNY_ROOT/backups/ with a timestamp.
# If migrations go wrong, the user can restore from this dump.

BACKUP_DIR="$LENNY_ROOT/backups"
mkdir -p "$BACKUP_DIR"

# Read DB credentials from .env
DB_NAME=$(grep -E '^DB_NAME=' "$LENNY_ROOT/.env" | cut -d= -f2- | tr -d '"'"'")
DB_USER=$(grep -E '^DB_USER=' "$LENNY_ROOT/.env" | cut -d= -f2- | tr -d '"'"'")

if [ -z "$DB_NAME" ] || [ -z "$DB_USER" ]; then
    echo "Could not read DB_NAME or DB_USER from .env, skipping backup."
    exit 0
fi

# Find the database container via compose (not hardcoded)
CONTAINER=$($COMPOSE_CMD -p "$LENNY_COMPOSE_PROJECT" -f "$LENNY_ROOT/compose.yaml" ps -q db 2>/dev/null || true)

if [ -z "$CONTAINER" ]; then
    echo "Database container is not running, skipping backup."
    echo "If this is a first-time setup, this is expected."
    exit 0
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql"

echo "Backing up database '${DB_NAME}' → ${BACKUP_FILE}..."

if docker exec "$CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" --clean --if-exists > "$BACKUP_FILE"; then
    # Verify the dump is not empty
    if [ -s "$BACKUP_FILE" ]; then
        SIZE=$(wc -c < "$BACKUP_FILE" | tr -d ' ')
        if tail -1 "$BACKUP_FILE" | grep -q "PostgreSQL database dump complete"; then
            echo "Backup verified (${SIZE} bytes): ${BACKUP_FILE}"
        else
            echo "Warning: backup may be incomplete (no footer found)."
            echo "Backup saved (${SIZE} bytes): ${BACKUP_FILE}"
        fi

        # Keep only the 5 most recent backups, remove older ones
        ls -t "$BACKUP_DIR"/*.sql 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null || true
    else
        echo "Warning: backup file is empty. Database may be new."
        rm -f "$BACKUP_FILE"
    fi
else
    echo "Warning: pg_dump failed. Continuing without backup."
    echo "If this is a new installation with no data, this is expected."
    rm -f "$BACKUP_FILE"
fi
