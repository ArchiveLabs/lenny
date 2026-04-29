#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────
# Lenny ↔ Open Library auth bootstrap
#
# Authenticates a Lenny instance against archive.org/openlibrary.org using
# the operator's IA email+password, stores the returned IA S3 keys in .env,
# and restarts the API container so the new credentials are picked up.
#
# USAGE
#   Interactive:
#       bash docker/utils/ol_configure.sh
#   Scripted:
#       OL_EMAIL=you@example.com OL_PASSWORD='…' bash docker/utils/ol_configure.sh
#   Non-interactive re-login (replaces existing credentials):
#       LENNY_DEFAULTS=1 OL_EMAIL=… OL_PASSWORD=… bash docker/utils/ol_configure.sh
#
# The password is piped to the container over stdin so it never appears in
# argv, environment of any child process, or `docker inspect`.
# ─────────────────────────────────────────────────────────────────────────

LENNY_ROOT="${LENNY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
ENV_FILE="$LENNY_ROOT/.env"
BACKUP_DIR="$LENNY_ROOT/backups"
CONTAINER="${LENNY_API_CONTAINER:-lenny_api}"
COMPOSE_FILE="$LENNY_ROOT/compose.yaml"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; NC=$'\033[0m'
info()  { printf '%s[ol-configure]%s %s\n' "$CYAN"   "$NC" "$*"; }
ok()    { printf '%s[ol-configure]%s %s\n' "$GREEN"  "$NC" "$*"; }
warn()  { printf '%s[ol-configure]%s %s\n' "$YELLOW" "$NC" "$*" >&2; }
error() { printf '%s[ol-configure]%s %s\n' "$RED"    "$NC" "$*" >&2; }

# ── Preflight
if [ ! -f "$ENV_FILE" ]; then
    error ".env not found at $ENV_FILE. Run 'make configure' first."
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

# Resolve docker compose command (matches update.sh convention).
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    error "Neither 'docker compose' nor 'docker-compose' is available."
    exit 1
fi

# ── .env helpers (in-place, never clobber unrelated lines)

# Read a single key's value (blank if absent).
env_get() {
    local key="$1"
    awk -v k="$key" -F'=' 'index($0, k "=") == 1 { sub("^" k "=", ""); print; exit }' "$ENV_FILE"
}

# Replace the value of KEY in-place (or append if missing).
# Writes to a sibling temp file and moves atomically; preserves unrelated lines
# byte-for-byte. chmod 600 is applied before the move so the new file is never
# world-readable, even briefly.
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

# ── Re-login detection and confirmation
CURRENT_USER="$(env_get OL_USERNAME)"
if [ -n "$CURRENT_USER" ]; then
    if [ "${LENNY_DEFAULTS:-0}" != "1" ]; then
        warn "Currently logged in as: ${CURRENT_USER}"
        warn "Continuing will replace these credentials."
        if [ -t 0 ]; then
            read -r -p "Continue? [y/N] " _reply
            _reply="$(printf '%s' "${_reply:-}" | tr '[:upper:]' '[:lower:]')"
            case "$_reply" in
                y|yes) ;;
                *) info "Aborted."; exit 0 ;;
            esac
        else
            error "Non-interactive re-login requires LENNY_DEFAULTS=1 to confirm."
            exit 1
        fi
    else
        info "Re-login confirmed by LENNY_DEFAULTS=1 (replacing ${CURRENT_USER})."
    fi
fi

# ── Collect credentials
OL_EMAIL="${OL_EMAIL:-}"
if [ -z "$OL_EMAIL" ]; then
    if [ -t 0 ]; then
        read -r -p "Open Library / Internet Archive email: " OL_EMAIL
    else
        error "OL_EMAIL is required in non-interactive mode."
        exit 1
    fi
fi

OL_PASSWORD="${OL_PASSWORD:-}"
if [ -z "$OL_PASSWORD" ]; then
    if [ -t 0 ]; then
        # -s suppresses echo; the trailing `echo` adds the newline the prompt swallowed.
        read -r -s -p "Password: " OL_PASSWORD
        echo
    else
        error "OL_PASSWORD is required in non-interactive mode."
        exit 1
    fi
fi

if [ -z "$OL_EMAIL" ] || [ -z "$OL_PASSWORD" ]; then
    error "Email and password must not be empty."
    exit 1
fi

# ── Call the bootstrap module inside the running container
info "Authenticating with archive.org as ${OL_EMAIL}..."

ERR_TMP="$(mktemp)"
# Always clean up — and always drop the in-memory password — on exit.
cleanup() { rm -f "$ERR_TMP"; unset OL_PASSWORD; }
trap cleanup EXIT

# Password is piped on stdin; argv carries only the (non-secret) email.
if ! auth_out="$(
    printf '%s' "$OL_PASSWORD" \
    | docker exec -i "$CONTAINER" python -m lenny.core.ol_bootstrap "$OL_EMAIL" 2>"$ERR_TMP"
)"; then
    err_line="$(tail -n1 "$ERR_TMP" 2>/dev/null || true)"
    # Expected format: ERROR:CODE:message
    rest="${err_line#ERROR:}"
    code="${rest%%:*}"
    case "$code" in
        INVALID_CREDENTIALS) error "Login failed: email or password is incorrect." ;;
        IA_UNREACHABLE)      error "Login failed: could not reach archive.org. Check your network." ;;
        MISSING_DEP)         error "Login failed: the 'internetarchive' package is missing in the container. Run 'make redeploy' to rebuild." ;;
        NO_KEYS)             error "Login failed: archive.org did not return S3 keys for this account." ;;
        BAD_EMAIL|BAD_PASSWORD) error "Login failed: ${rest#*:}" ;;
        *) error "Login failed: ${err_line:-unknown error}" ;;
    esac
    exit 2
fi

# Password no longer needed — drop it now, even though `cleanup` will also unset.
unset OL_PASSWORD

# ── Parse the three newline-separated values from stdout
{ IFS= read -r access || true; IFS= read -r secret || true; IFS= read -r screenname || true; } <<EOF
$auth_out
EOF

if [ -z "${access:-}" ] || [ -z "${secret:-}" ]; then
    error "archive.org returned an unexpected response (no S3 keys)."
    exit 3
fi

# ── Persist to .env (backup first; atomic rewrite)
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
backup_file="$BACKUP_DIR/.env.$(date +%Y%m%d_%H%M%S).bak"
cp "$ENV_FILE" "$backup_file"
chmod 600 "$backup_file"
info "Backed up .env → ${backup_file#${LENNY_ROOT}/}"

env_set OL_S3_ACCESS_KEY "$access"
env_set OL_S3_SECRET_KEY "$secret"
env_set OL_USERNAME "$OL_EMAIL"
# Completing auth means lending is now functional; flip the flag on.
env_set LENNY_LENDING_ENABLED "true"
chmod 600 "$ENV_FILE"

# ── Restart API so the new env is picked up
info "Restarting ${CONTAINER} so the new credentials take effect..."
if $COMPOSE_CMD -p lenny -f "$COMPOSE_FILE" restart "$CONTAINER" >/dev/null 2>&1; then
    ok "Logged in as ${screenname:-$OL_EMAIL}. Lending is now enabled."
else
    warn "Credentials saved, but failed to restart ${CONTAINER}. Run 'make restart' manually."
fi
