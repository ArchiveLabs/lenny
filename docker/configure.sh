#!/usr/bin/env bash

LENNY_ENV_FILE=".env"
READER_ENV_FILE="reader.env"
AUTH_ENV_FILE="auth.env"
OL_ENV_FILE="ol.env"
LOAN_ENV_FILE="loan.env"

genpass() {
    len=${1:-32}
    local pass
    pass=$(dd if=/dev/urandom bs=1 count=$((len * 2)) 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | head -c "$len")
    if [ "${#pass}" -ne "$len" ]; then
        echo "genpass: expected $len chars, got ${#pass}" >&2
        return 1
    fi
    echo "$pass"
}

# Garage's rpc_secret must be valid hex, unlike genpass()'s alphanumeric output.
# Prefers openssl (single well-tested call) over the dd|od pipeline, and
# validates output length instead of trusting it — this script has no
# `set -e`, so a silent truncation here would write an invalid Garage RPC
# secret straight into .env with nothing to catch it.
genhex() {
    bytes=${1:-32}
    local hex
    if command -v openssl >/dev/null 2>&1; then
        hex=$(openssl rand -hex "$bytes")
    else
        hex=$(dd if=/dev/urandom bs=1 count="$bytes" 2>/dev/null | od -An -tx1 | tr -d ' \n')
    fi
    if [ "${#hex}" -ne $((bytes * 2)) ]; then
        echo "genhex: expected $((bytes * 2)) hex chars, got ${#hex}" >&2
        return 1
    fi
    echo "$hex"
}

# Exit if the file already exists
if [ -f "$LENNY_ENV_FILE" ]; then
  echo "Skipping configure: $LENNY_ENV_FILE already configured."
else
  echo "Creating $LENNY_ENV_FILE."
 
  # Use environment variables if they are set, otherwise provide defaults or generate secure values
  LENNY_HOST="localhost"
  LENNY_PORT="${LENNY_PORT:-8080}"
  LENNY_WORKERS="${LENNY_WORKERS:-2}"
  LENNY_ADMIN_NODE_HEAP_MB="${LENNY_ADMIN_NODE_HEAP_MB:-384}"
  LENNY_LOG_LEVEL="${LENNY_LOG_LEVEL:-debug}"
  LENNY_PRODUCTION="${LENNY_PRODUCTION:-true}"
  LENNY_SSL_CRT="${LENNY_SSL_CRT:-}"
  LENNY_SSL_KEY="${LENNY_SSL_KEY:-}"
  LENNY_SEED="${LENNY_SEED:-$(genpass 32)}"
  # Base URL of the Lenny instance as seen by the browser (no /v1/api suffix —
  # the admin UI appends that itself). Leave empty for same-origin deployments
  # behind nginx, or set an absolute URL (https://library.example.com) for
  # external/custom-domain deployments.
  NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-}"
  OTP_SERVER="${OTP_SERVER:-https://openlibrary.org}"
  BRIET_REDEEM_URL="${BRIET_REDEEM_URL:-https://demo.market.briet.app/api/redeem-lenny}"

  READER_PORT="${READER_PORT:-3000}"
  READIUM_PORT="${READIUM_PORT:-15080}"

  MANIFEST_ROUTE_FORCE_ENABLE="${MANIFEST_ROUTE_FORCE_ENABLE:-true}"
  MANIFEST_ALLOWED_DOMAINS="${MANIFEST_ALLOWED_DOMAINS:-127.0.0.1,localhost,*.trycloudflare.com}"
  NODE_ENV="${NODE_ENV:-production}"

  DB_USER="${POSTGRES_USER:-librarian}"
  DB_HOST="${POSTGRES_HOST:-127.0.0.1}"
  DB_PORT="${POSTGRES_PORT:-5432}"

  DB_PASSWORD="${POSTGRES_PASSWORD:-$(genpass 32)}"
  DB_NAME="${DB_NAME:-lenny}"

  S3_ACCESS_KEY="${S3_ACCESS_KEY:-$(genpass 20)}"
  S3_SECRET_KEY="${S3_SECRET_KEY:-$(genpass 40)}"
  S3_ENDPOINT="${S3_ENDPOINT:-http://s3:3900}"
  S3_REGION="${S3_REGION:-garage}"
  S3_RPC_SECRET="${S3_RPC_SECRET:-$(genhex 32)}"
  S3_ADMIN_TOKEN="${S3_ADMIN_TOKEN:-$(genpass 32)}"
  # This script has no `set -e` — a failed genhex/genpass above would
  # otherwise write an empty secret into .env with no error at all. Garage
  # refuses to start on an invalid RPC secret, so fail loudly here instead.
  if [ -z "$S3_RPC_SECRET" ] || [ -z "$S3_ADMIN_TOKEN" ]; then
      echo "configure.sh: failed to generate S3_RPC_SECRET/S3_ADMIN_TOKEN." >&2
      exit 1
  fi

  # Write to lenny.env
  cat <<EOF > "$LENNY_ENV_FILE"
# API
LENNY_PROXY=
LENNY_HOST=$LENNY_HOST
LENNY_PORT=$LENNY_PORT
LENNY_WORKERS=$LENNY_WORKERS
LENNY_ADMIN_NODE_HEAP_MB=$LENNY_ADMIN_NODE_HEAP_MB
LENNY_SEED=$LENNY_SEED
LENNY_LOG_LEVEL=$LENNY_LOG_LEVEL
LENNY_PRODUCTION=$LENNY_PRODUCTION
LENNY_SSL_CRT=$LENNY_SSL_CRT
LENNY_SSL_KEY=$LENNY_SSL_KEY
OTP_SERVER=$OTP_SERVER
# BRIET bundle-redeem endpoint. Defaults to BRIET's DEMO marketplace, which
# behaves like production but hands out throwaway codes — swap in the real
# host (market.briet.app) once that side is live.
BRIET_REDEEM_URL=$BRIET_REDEEM_URL
# Set to an absolute URL for custom-domain deployments, e.g. https://library.example.com/v1/api
NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

# Service Ports
READER_PORT=$READER_PORT
READIUM_PORT=$READIUM_PORT

# DB
DB_USER=$DB_USER
DB_HOST=$DB_HOST
DB_PORT=$DB_PORT
DB_PASSWORD=$DB_PASSWORD
DB_NAME=$DB_NAME
DB_TYPE=postgres

# S3 Credentials
S3_ACCESS_KEY=$S3_ACCESS_KEY
S3_SECRET_KEY=$S3_SECRET_KEY
S3_ENDPOINT=$S3_ENDPOINT
S3_REGION=$S3_REGION
S3_RPC_SECRET=$S3_RPC_SECRET
S3_ADMIN_TOKEN=$S3_ADMIN_TOKEN
S3_PROVIDER=garage
S3_SECURE=false
# Fresh installs never had MinIO data to migrate — the update-engine's
# migration step (025_migrate_s3_to_garage.sh) no-ops when this is set.
S3_GARAGE_MIGRATED=true

# OPDS redirect allowlist — comma-separated hostnames allowed as https:// redirect_uri
# in the OPDS OAuth flow (e.g. my.opds.client.com). Leave empty to block all https:// redirects.
# Lenny's own bundled reader is served from this same origin under /read and uses relative
# paths, so it needs no entry here. A browser-based OPDS client hosted on a DIFFERENT origin
# (e.g. reader.archive.org) does: without its host listed, its patrons will sign in
# successfully and then never be redirected back to it. See docs/OAUTH.md.
LENNY_OPDS_ALLOWED_HOSTS=

EOF
  # .env holds secrets (admin password, DB password, S3 keys, IA S3 keys).
  # Restrict to owner-only read/write.
  chmod 600 "$LENNY_ENV_FILE"
fi

# Exit if the file already exists
if [ -f "$READER_ENV_FILE" ]; then
  echo "Skipping configure: $READER_ENV_FILE already configured."
else
  echo "Creating $READER_ENV_FILE"
  cat <<EOF > "$READER_ENV_FILE"
# Reader
MANIFEST_ROUTE_FORCE_ENABLE=$MANIFEST_ROUTE_FORCE_ENABLE
MANIFEST_ALLOWED_DOMAINS=$MANIFEST_ALLOWED_DOMAINS
NODE_ENV=$NODE_ENV

EOF
fi

# ── auth.env: admin credentials + external OAuth provider ────────────────────
if [ -f "$AUTH_ENV_FILE" ]; then
  echo "Skipping configure: $AUTH_ENV_FILE already configured."
else
  echo "Creating $AUTH_ENV_FILE."

  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(genpass 32)}"
  ADMIN_INTERNAL_SECRET="${ADMIN_INTERNAL_SECRET:-$(genpass 32)}"
  ADMIN_SALT="${ADMIN_SALT:-$(genpass 32)}"

  # External OAuth / OIDC provider credentials (all optional — empty = disabled)
  LENNY_EXTERNAL_AUTH_ENABLED="${LENNY_EXTERNAL_AUTH_ENABLED:-false}"
  OAUTH_CLIENT_ID="${OAUTH_CLIENT_ID:-}"
  OAUTH_CLIENT_SECRET="${OAUTH_CLIENT_SECRET:-}"
  OAUTH_DISCOVERY_URL="${OAUTH_DISCOVERY_URL:-}"
  OAUTH_REDIRECT_URI="${OAUTH_REDIRECT_URI:-}"
  OAUTH_SCOPES="${OAUTH_SCOPES:-openid email profile}"
  OAUTH_FLOW="pkce"  # only supported flow; implicit and plain authorization_code removed
  IA_AUTH_ENABLED="${IA_AUTH_ENABLED:-false}"

  cat <<EOF > "$AUTH_ENV_FILE"
# Admin credentials
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_PASSWORD=$ADMIN_PASSWORD
ADMIN_INTERNAL_SECRET=$ADMIN_INTERNAL_SECRET
ADMIN_SALT=$ADMIN_SALT

# External OAuth / OIDC provider (optional)
# Set LENNY_EXTERNAL_AUTH_ENABLED=true and fill in the provider details to
# enable an alternative patron auth path alongside the existing OTP flow.
# Any OIDC-compliant provider works (Clerk, Auth0, Okta, Keycloak, Google …).
LENNY_EXTERNAL_AUTH_ENABLED=$LENNY_EXTERNAL_AUTH_ENABLED
OAUTH_CLIENT_ID=$OAUTH_CLIENT_ID
OAUTH_CLIENT_SECRET=$OAUTH_CLIENT_SECRET
OAUTH_DISCOVERY_URL=$OAUTH_DISCOVERY_URL
OAUTH_REDIRECT_URI=$OAUTH_REDIRECT_URI
OAUTH_SCOPES=$OAUTH_SCOPES
OAUTH_FLOW=$OAUTH_FLOW

# IA S3 patron authentication (optional)
# Set IA_AUTH_ENABLED=true to allow patrons to authenticate with their
# Internet Archive S3 access/secret key pair instead of the OTP flow.
IA_AUTH_ENABLED=$IA_AUTH_ENABLED

EOF
  # auth.env holds admin passwords and OAuth client secrets — owner-only.
  chmod 600 "$AUTH_ENV_FILE"
fi

# ── ol.env: Open Library / Internet Archive lending credentials ───────────────
if [ -f "$OL_ENV_FILE" ]; then
  echo "Skipping configure: $OL_ENV_FILE already configured."
else
  echo "Creating $OL_ENV_FILE."

  # Empty by default — populated by `make ol-login` (see docker/utils/ol_configure.sh).
  # The API degrades gracefully to anonymous OL calls when these are blank.
  OL_S3_ACCESS_KEY="${OL_S3_ACCESS_KEY:-}"
  OL_S3_SECRET_KEY="${OL_S3_SECRET_KEY:-}"
  OL_USERNAME="${OL_USERNAME:-}"
  # none | ol | external  — set to "ol" automatically by `make ol-login`
  LENNY_LENDING_MODE="${LENNY_LENDING_MODE:-none}"
  LENNY_OL_INDEXED="${LENNY_OL_INDEXED:-false}"

  cat <<EOF > "$OL_ENV_FILE"
# Open Library / Internet Archive credentials
# Credentials are populated by \`make ol-login\`; empty values mean anonymous OL access.
# LENNY_LENDING_MODE controls which lending provider is active: none | ol | external
OL_S3_ACCESS_KEY=$OL_S3_ACCESS_KEY
OL_S3_SECRET_KEY=$OL_S3_SECRET_KEY
OL_USERNAME=$OL_USERNAME
LENNY_LENDING_MODE=$LENNY_LENDING_MODE
LENNY_OL_INDEXED=$LENNY_OL_INDEXED

EOF
  # ol.env holds IA S3 keys — restrict to owner-only.
  chmod 600 "$OL_ENV_FILE"
fi

# ── loan.env: runtime-editable loan policy ───────────────────────────────────
# Lives in its own file (not .env) because the admin UI rewrites it at runtime
# via /admin/loan/settings. Keeping it out of .env avoids touching secrets on
# every policy edit. Not sensitive — no chmod 600.
if [ -f "$LOAN_ENV_FILE" ]; then
  echo "Skipping configure: $LOAN_ENV_FILE already configured."
else
  echo "Creating $LOAN_ENV_FILE."

  # Reuse existing values if present (e.g. exported from a legacy .env during
  # `make update` bootstrap); otherwise fall back to defaults.
  LENNY_LOAN_LIMIT="${LENNY_LOAN_LIMIT:-10}"
  LENNY_LOAN_DURATION_DAYS="${LENNY_LOAN_DURATION_DAYS:-0}"

  cat <<EOF > "$LOAN_ENV_FILE"
# Loan policy — runtime-editable from the admin UI (/admin/loan/settings).
# LENNY_LOAN_LIMIT: max concurrent loans per patron (>=1).
# LENNY_LOAN_DURATION_DAYS: days until auto-return; 0 = never expire.
LENNY_LOAN_LIMIT=$LENNY_LOAN_LIMIT
LENNY_LOAN_DURATION_DAYS=$LENNY_LOAN_DURATION_DAYS

EOF
fi

# Install 'lenny' CLI command if not already available
LENNY_PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if ! command -v lenny &>/dev/null; then
  INSTALL_DIR="$HOME/.local/bin"
  mkdir -p "$INSTALL_DIR"
  cat > "$INSTALL_DIR/lenny" <<SCRIPT
#!/bin/sh
make -C "$LENNY_PROJECT_DIR" "\$@"
SCRIPT
  chmod +x "$INSTALL_DIR/lenny"

  case ":$PATH:" in
    *":$INSTALL_DIR:"*)
      echo "[lenny] CLI installed. You can now use: lenny start, lenny stop, etc."
      ;;
    *)
      echo "[lenny] CLI installed to $INSTALL_DIR/lenny"
      echo "[lenny] Add to PATH: echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
      ;;
  esac
else
  echo "Skipping CLI install: 'lenny' command already available."
fi
