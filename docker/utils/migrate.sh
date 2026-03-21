#!/bin/sh
# Lenny database migration runner
# Checks and applies pending Alembic migrations.

set -e

echo "[lenny] Checking database migrations..."

# Capture current revision before upgrade
CURRENT=$(alembic current 2>/dev/null | grep -oE '[a-f0-9]+' | head -1 || echo "none")
HEAD=$(alembic heads 2>/dev/null | grep -oE '[a-f0-9]+' | head -1 || echo "unknown")

if [ "$CURRENT" = "$HEAD" ] && [ "$CURRENT" != "none" ]; then
    echo "[lenny] Database is up to date (revision: $CURRENT). No migrations to apply."
else
    if [ "$CURRENT" = "none" ]; then
        echo "[lenny] No migration history found. Applying all migrations..."
    else
        echo "[lenny] Database at revision $CURRENT, head is $HEAD. Applying pending migrations..."
    fi

    alembic upgrade head

    NEW=$(alembic current 2>/dev/null | grep -oE '[a-f0-9]+' | head -1 || echo "unknown")
    echo "[lenny] Migrations complete. Database now at revision: $NEW"
fi
