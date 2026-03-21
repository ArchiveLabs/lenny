#!/usr/bin/env bash
set -euo pipefail

# ── Lenny Update Engine ─────────────────────────────────────────────
# Orchestrator: pre-flight → git pull → global pipeline → stamp version.
#
# The engine pulls the latest code, syncs environment variables,
# backs up the database, rebuilds containers, and stamps the version.
# All changes ship in PRs — no manual migration scripts needed.
#
# See: docs/plans/update-engine.md
# Usage: make update
# ─────────────────────────────────────────────────────────────────────

LENNY_ROOT="$(git rev-parse --show-toplevel)"
export LENNY_ROOT
export LENNY_COMPOSE_PROJECT="lenny"

VERSION_FILE="$LENNY_ROOT/VERSION"
INSTALLED_VERSION_FILE="$LENNY_ROOT/.lenny-version"
PIPELINE_DIR="$LENNY_ROOT/docker/utils/update"

# ── Colors ───────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()  { echo -e "${CYAN}[update]${NC} $*"; }
warn()  { echo -e "${YELLOW}[update]${NC} $*"; }
error() { echo -e "${RED}[update]${NC} $*"; }
ok()    { echo -e "${GREEN}[update]${NC} $*"; }

# ── Lock — Prevent Concurrent Updates ─────────────────────────────
LOCK_DIR="/tmp/lenny-update.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    error "Another update is already running (or stale lock at $LOCK_DIR)."
    error "If no other update is running, remove: $LOCK_DIR"
    exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null' EXIT

# ── Banner ────────────────────────────────────────────────────────
echo ""
info "====================================="
info "       Lenny Update Engine"
info "====================================="
warn "Do NOT close this terminal, shut down"
warn "your machine, or stop Docker while the"
warn "update is running. Interrupting may"
warn "leave your installation in a broken state."
echo ""

# ── Step 1: Pre-flight Checks ───────────────────────────────────────

info "[Step 1/4] Running pre-flight checks..."

# 1. Docker daemon running
if ! docker info > /dev/null 2>&1; then
    error "Docker is not running. Start Docker and try again."
    exit 1
fi

# 2. .env exists
if [ ! -f "$LENNY_ROOT/.env" ]; then
    error ".env file not found."
    error "If this is a fresh install, run: make configure"
    error "If you are updating, restore your .env file first."
    exit 1
fi

# 3. Working tree clean
if [ -n "$(git -C "$LENNY_ROOT" status --porcelain)" ]; then
    error "Working tree has uncommitted changes."
    error "To continue, either:"
    error "  git stash        (saves changes, apply later with: git stash pop)"
    error "  git commit -am   (commit your changes first)"
    exit 1
fi

# 4. Compose command available
if docker compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
else
    error "Neither 'docker compose' nor 'docker-compose' found."
    exit 1
fi
export COMPOSE_CMD

ok "Pre-flight checks passed."

# ── Step 2: Git Pull + Version Detection ─────────────────────────────

info "[Step 2/4] Pulling latest code..."
if ! git -C "$LENNY_ROOT" pull --ff-only; then
    error "git pull --ff-only failed. Your local branch has diverged."
    error "Resolve manually (e.g., git rebase) then re-run: make update"
    exit 1
fi

if [ ! -f "$VERSION_FILE" ]; then
    error "VERSION file not found. Repository may be corrupted."
    exit 1
fi

LENNY_TARGET_VERSION="$(cat "$VERSION_FILE" | tr -d '[:space:]')"
export LENNY_TARGET_VERSION

if [ -f "$INSTALLED_VERSION_FILE" ]; then
    LENNY_INSTALLED_VERSION="$(cat "$INSTALLED_VERSION_FILE" | tr -d '[:space:]')"
else
    LENNY_INSTALLED_VERSION="unset"
fi
export LENNY_INSTALLED_VERSION

# Already up to date?
if [ "$LENNY_INSTALLED_VERSION" = "$LENNY_TARGET_VERSION" ]; then
    ok "Already up to date (v${LENNY_TARGET_VERSION})."
    exit 0
fi

info "Updating: ${LENNY_INSTALLED_VERSION} → ${LENNY_TARGET_VERSION}"

# ── Step 3: Global Pipeline ─────────────────────────────────────────
# These scripts always run on every update.
# They bring the environment into a consistent state.

info "[Step 3/4] Running global pipeline (sync, backup, build, restart)..."
warn "This step rebuilds containers — it may take a few minutes."

for script in "$PIPELINE_DIR"/[0-9]*.sh; do
    [ -f "$script" ] || continue

    info "Running $(basename "$script")..."
    if ! bash "$script"; then
        error "$(basename "$script") FAILED — fix the issue and re-run: make update"
        exit 1
    fi
done

# ── Step 4: Stamp Version ───────────────────────────────────────────

info "[Step 4/4] Finalizing..."
echo "$LENNY_TARGET_VERSION" > "$INSTALLED_VERSION_FILE"

echo ""
ok "====================================="
ok "  Update complete!"
ok "  ${LENNY_INSTALLED_VERSION} → ${LENNY_TARGET_VERSION}"
ok "====================================="
echo ""
