#!/usr/bin/env bash

LENNY_ENV_FILE=".env"
READER_ENV_FILE="reader.env"

genpass() {
    len=${1:-32}
    dd if=/dev/urandom bs=1 count=$((len * 2)) 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | head -c "$len"
}

# Exit if the file already exists
if [ -f "$LENNY_ENV_FILE" ]; then
  echo "Skipping configure: $LENNY_ENV_FILE already configured."
else
  echo "Creating $LENNY_ENV_FILE."
 
  # Use environment variables if they are set, otherwise provide defaults or generate secure values
  LENNY_HOST="localhost"
  LENNY_PORT="${LENNY_PORT:-8080}"
  LENNY_WORKERS="${LENNY_WORKERS:-3}"
  LENNY_LOG_LEVEL="${LENNY_LOG_LEVEL:-debug}"
  LENNY_PRODUCTION="${LENNY_PRODUCTION:-true}"
  LENNY_SSL_CRT="${LENNY_SSL_CRT:-}"
  LENNY_SSL_KEY="${LENNY_SSL_KEY:-}"
  LENNY_SEED="${LENNY_SEED:-$(genpass 32)}"
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
  ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(genpass 32)}"
  ADMIN_INTERNAL_SECRET="${ADMIN_INTERNAL_SECRET:-$(genpass 32)}"
  ADMIN_SALT="${ADMIN_SALT:-$(genpass 32)}"
  # Public URL of the Lenny API as seen by the browser.
  # Use a relative path (/v1/api) when the admin UI is served behind the same
  # nginx, or set an absolute URL (https://library.example.com/v1/api) for
  # external/custom-domain deployments.
  NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-/v1/api}"
  OTP_SERVER="${OTP_SERVER:-https://openlibrary.org}"
  LENNY_LOAN_LIMIT="${LENNY_LOAN_LIMIT:-10}"

  # Open Library / Internet Archive credentials.
  # Populated by `lenny ol-configure` (see docker/utils/ol_configure.sh).
  # Empty by default — the API degrades gracefully to anonymous OL calls.
  OL_S3_ACCESS_KEY="${OL_S3_ACCESS_KEY:-}"
  OL_S3_SECRET_KEY="${OL_S3_SECRET_KEY:-}"
  OL_USERNAME="${OL_USERNAME:-}"
  LENNY_LENDING_ENABLED="${LENNY_LENDING_ENABLED:-false}"
  LENNY_OL_INDEXED="${LENNY_OL_INDEXED:-false}"

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

  S3_ACCESS_KEY="${MINIO_ROOT_USER:-$(genpass 20)}"
  S3_SECRET_KEY="${MINIO_ROOT_PASSWORD:-$(genpass 40)}"
  S3_ENDPOINT="${S3_ENDPOINT:-http://s3:9000}"

  # Write to lenny.env
  cat <<EOF > "$LENNY_ENV_FILE"
# API
LENNY_PROXY=
LENNY_HOST=$LENNY_HOST
LENNY_PORT=$LENNY_PORT
LENNY_WORKERS=$LENNY_WORKERS
LENNY_SEED=$LENNY_SEED
LENNY_LOG_LEVEL=$LENNY_LOG_LEVEL
LENNY_PRODUCTION=$LENNY_PRODUCTION
LENNY_SSL_CRT=$LENNY_SSL_CRT
LENNY_SSL_KEY=$LENNY_SSL_KEY
OTP_SERVER=$OTP_SERVER
ADMIN_USERNAME=$ADMIN_USERNAME
ADMIN_PASSWORD=$ADMIN_PASSWORD
ADMIN_INTERNAL_SECRET=$ADMIN_INTERNAL_SECRET
ADMIN_SALT=$ADMIN_SALT

# Open Library Authentication (IA S3 keys)
# Populated by `lenny ol-configure`; empty values mean anonymous OL access.
OL_S3_ACCESS_KEY=$OL_S3_ACCESS_KEY
OL_S3_SECRET_KEY=$OL_S3_SECRET_KEY
OL_USERNAME=$OL_USERNAME
LENNY_LENDING_ENABLED=$LENNY_LENDING_ENABLED
LENNY_OL_INDEXED=$LENNY_OL_INDEXED
# Set to an absolute URL for custom-domain deployments, e.g. https://library.example.com/v1/api
NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

# Loan Limit
LENNY_LOAN_LIMIT=$LENNY_LOAN_LIMIT

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
S3_PROVIDER=minio
S3_SECURE=false

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
