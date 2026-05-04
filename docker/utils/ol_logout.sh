#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────
# Lenny ↔ Open Library auth teardown
#
# Clears the IA S3 keys and username from .env, disables lending, and
# restarts the API container so the changes are picked up immediately.
#
# USAGE
#   Interactive:
#       make ol-logout
#   Non-interactive (skip confirmation):
#       LENNY_NONINTERACTIVE=1 bash docker/utils/ol_logout.sh
# ─────────────────────────────────────────────────────────────────────────

LENNY_ROOT="${LENNY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
ENV_FILE="$LENNY_ROOT/.env"
CONTAINER="${LENNY_API_CONTAINER:-lenny_api}"
COMPOSE_FILE="$LENNY_ROOT/compose.yaml"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; NC=$'\033[0m'
info()  { printf '%s[ol-logout]%s %s\n' "$CYAN"   "$NC" "$*"; }
ok()    { printf '%s[ol-logout]%s %s\n' "$GREEN"  "$NC" "$*"; }
warn()  { printf '%s[ol-logout]%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
error() { printf '%s[ol-logout]%s %s\n' "$RED"    "$NC" "$*" >&2; }

# ── Preflight
if [ ! -f "$ENV_FILE" ]; then
    error ".env not found at $ENV_FILE. Nothing to clear."
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    error "docker is required but not installed."
    exit 1
fi
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    error "Container '$CONTAINER' is not running. Start Lenny first ('make start' or 'make rebuild')."
    exit 1
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    error "Neither 'docker compose' nor 'docker-compose' is available."
    exit 1
fi

# ── .env helpers (same pattern as ol_configure.sh)
env_get() {
    local key="$1"
    awk -v k="$key" -F'=' 'index($0, k "=") == 1 { sub("^" k "=", ""); print; exit }' "$ENV_FILE"
}

env_set() {
    local key="$1" value="$2" tmp found=0
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    chmod 600 "$tmp"
    while IFS= read -r line || [ -n "$line" ]; do
        if [ "${line%%=*}" = "$key" ] && [ "${line#*=}" != "$line" ]; then
            printf '%s=%s\n' "$key" "$value" >> "$tmp"
            found=1
        else
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$ENV_FILE"
    [ "$found" -eq 1 ] || printf '%s=%s\n' "$key" "$value" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
}

# ── Check if logged in
CURRENT_USER="$(env_get OL_USERNAME)"
if [ -z "$CURRENT_USER" ]; then
    warn "No Open Library credentials are configured. Nothing to do."
    exit 0
fi

# ── Confirm
if [ "${LENNY_NONINTERACTIVE:-0}" != "1" ]; then
    warn "Currently logged in as: ${CURRENT_USER}"
    warn "This will clear your IA S3 keys and disable lending."
    if [ -t 0 ]; then
        read -r -p "Continue? [y/N] " _reply
        _reply="$(printf '%s' "${_reply:-}" | tr '[:upper:]' '[:lower:]')"
        case "$_reply" in
            y|yes) ;;
            *) info "Aborted."; exit 0 ;;
        esac
    else
        error "Non-interactive logout requires LENNY_NONINTERACTIVE=1 to confirm."
        exit 1
    fi
else
    info "Logout confirmed by LENNY_NONINTERACTIVE=1 (clearing ${CURRENT_USER})."
fi

# ── Clear credentials and disable lending
env_set OL_S3_ACCESS_KEY ""
env_set OL_S3_SECRET_KEY ""
env_set OL_USERNAME ""
env_set LENNY_LENDING_ENABLED "false"
chmod 600 "$ENV_FILE"

# ── Restart API so cleared credentials take effect
info "Restarting ${CONTAINER} so the cleared credentials take effect..."
if $COMPOSE_CMD -p lenny -f "$COMPOSE_FILE" up -d --no-deps api >/dev/null 2>&1; then
    ok "Logged out of ${CURRENT_USER}. Lending is now disabled."
else
    warn "Credentials cleared, but failed to restart ${CONTAINER}. Run 'make restart' manually."
fi
