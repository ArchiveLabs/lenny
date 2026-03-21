#!/usr/bin/env bash
set -euo pipefail

# ── Lenny Doctor 
# Quick health check for the Lenny environment.
# Usage: make doctor

LENNY_ROOT="$(git rev-parse --show-toplevel)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "  ${GREEN}✓${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }

ERRORS=0

echo "Lenny Health Check"
echo "==================="
echo ""

# 1. Docker running
if docker info > /dev/null 2>&1; then
    pass "Docker is running"
else
    fail "Docker is not running"
    ERRORS=$((ERRORS + 1))
fi

# 2. Compose command available
if docker compose version >/dev/null 2>&1; then
    pass "docker compose available ($(docker compose version --short 2>/dev/null || echo 'v2'))"
elif command -v docker-compose >/dev/null 2>&1; then
    pass "docker-compose available (legacy v1)"
else
    fail "No compose command found"
    ERRORS=$((ERRORS + 1))
fi

# 3. .env exists
if [ -f "$LENNY_ROOT/.env" ]; then
    pass ".env file exists"

    # Check required keys
    REQUIRED_KEYS="DB_NAME DB_USER DB_PASSWORD"
    for key in $REQUIRED_KEYS; do
        if grep -qE "^${key}=" "$LENNY_ROOT/.env"; then
            pass "  $key is set"
        else
            fail "  $key is missing from .env"
            ERRORS=$((ERRORS + 1))
        fi
    done
else
    fail ".env file not found (run: make configure)"
    ERRORS=$((ERRORS + 1))
fi

# 4. VERSION file
if [ -f "$LENNY_ROOT/VERSION" ]; then
    TARGET_VERSION="$(cat "$LENNY_ROOT/VERSION" | tr -d '[:space:]')"
    pass "VERSION file: $TARGET_VERSION"
else
    fail "VERSION file not found"
    ERRORS=$((ERRORS + 1))
fi

# 5. Installed version
if [ -f "$LENNY_ROOT/.lenny-version" ]; then
    INSTALLED_VERSION="$(cat "$LENNY_ROOT/.lenny-version" | tr -d '[:space:]')"
    if [ "${INSTALLED_VERSION:-}" = "${TARGET_VERSION:-}" ]; then
        pass "Installed version: $INSTALLED_VERSION (up to date)"
    else
        warn "Installed version: $INSTALLED_VERSION (target: ${TARGET_VERSION:-unknown})"
    fi
else
    warn "No .lenny-version file (first update not yet run)"
fi

# 6. Database reachable
if docker info > /dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    docker compose version >/dev/null 2>&1 || COMPOSE_CMD="docker-compose"
    CONTAINER=$($COMPOSE_CMD -p lenny -f "$LENNY_ROOT/compose.yaml" ps -q db 2>/dev/null || true)
    if [ -n "$CONTAINER" ]; then
        DB_USER=$(grep -E '^DB_USER=' "$LENNY_ROOT/.env" 2>/dev/null | cut -d= -f2- || echo "")
        if [ -n "$DB_USER" ] && docker exec "$CONTAINER" pg_isready -U "$DB_USER" > /dev/null 2>&1; then
            pass "Database is reachable"
        else
            warn "Database container exists but is not accepting connections"
        fi
    else
        warn "Database container is not running"
    fi
fi

# 7. Disk space
AVAILABLE_KB=$(df -k "$LENNY_ROOT" | awk 'NR==2 {print $4}')
AVAILABLE_GB=$((AVAILABLE_KB / 1024 / 1024))
if [ "$AVAILABLE_GB" -ge 5 ]; then
    pass "Disk space: ${AVAILABLE_GB}GB available"
elif [ "$AVAILABLE_GB" -ge 1 ]; then
    warn "Disk space: ${AVAILABLE_GB}GB available (low)"
else
    fail "Disk space: ${AVAILABLE_GB}GB available (critically low)"
    ERRORS=$((ERRORS + 1))
fi

echo ""
if [ "$ERRORS" -eq 0 ]; then
    echo -e "${GREEN}All checks passed.${NC}"
else
    echo -e "${RED}${ERRORS} check(s) failed.${NC}"
    exit 1
fi
