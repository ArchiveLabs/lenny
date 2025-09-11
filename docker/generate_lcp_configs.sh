#!/bin/sh
# Generates config.yaml and lsd_config.yaml for Readium LCP/LSD servers using environment variables from .env

set -e

ENV_FILE="./.env"

# Function to safely load environment variables from .env file
load_env() {
    if [ -f "$ENV_FILE" ]; then
        # Clear any previously set variables that might interfere
        unset LENNY_HOST LENNY_PORT LCP_HOST LCP_PORT LCP_PUBLIC_BASE_URL
        unset LCP_DB_NAME LCP_UPDATE_USER LCP_UPDATE_PASS LCP_HTPASSWD_USER LCP_HTPASSWD_PASS
        unset LSD_HOST LSD_PORT LSD_PUBLIC_BASE_URL LSD_NOTIFY_USER LSD_NOTIFY_PASS
        unset DB_USER DB_HOST DB_PORT DB_PASSWORD DB_NAME
        unset S3_ACCESS_KEY S3_SECRET_KEY S3_ENDPOINT S3_PROVIDER S3_SECURE
        unset READER_PORT READIUM_PORT LENNY_WORKERS LENNY_LOG_LEVEL LENNY_RELOAD LENNY_SSL_CRT LENNY_SSL_KEY
        
        # Load fresh values from .env file
        set -a  # automatically export all variables
        . "$ENV_FILE"
        set +a  # stop automatically exporting
    else
        echo "Error: $ENV_FILE not found!"
        exit 1
    fi
}

# Load environment variables
load_env

CONFIG_DIR="./readium/config"

cat > "$CONFIG_DIR/config.yaml" <<EOF
profile: "basic"
lcp:
    host: "${LCP_HOST}"
    port: ${LCP_PORT}
    public_base_url: "${LCP_PUBLIC_BASE_URL}"
    database: "postgres://${DB_USER}:${DB_PASSWORD}@db:${DB_PORT}/${LCP_DB_NAME}?sslmode=disable"
    auth_file: "/srv/config/htpasswd"
storage:
    mode: "fs"
    filesystem:
        directory: "/srv/tmp"
        url: "http://${LENNY_HOST}:${LENNY_PORT}/static"
certificate:
    cert: "/srv/config/cert-edrlab-test.pem"
    private_key: "/srv/config/privkey-edrlab-test.pem"
license:
    links:
        status: "http://${LSD_HOST}:${LSD_PORT}/lcp/licenses/{license_id}/status"
        hint: "http://${LENNY_HOST}:${LENNY_PORT}/static/lcp_hint.html"
lsd:
    public_base_url:  "${LSD_PUBLIC_BASE_URL}"
lsd_notify_auth:
    username: "${LSD_NOTIFY_USER}"
    password: "${LSD_NOTIFY_PASS}"
EOF

cat > "$CONFIG_DIR/lsd_config.yaml" <<EOF
lsd:
  host: "${LSD_HOST}"
  port: ${LSD_PORT}
  public_base_url: "${LSD_PUBLIC_BASE_URL}"
  database: "postgres://${DB_USER}:${DB_PASSWORD}@db:${DB_PORT}/${LCP_DB_NAME}?sslmode=disable"
  auth_file: "/srv/config/htpasswd"
  license_link_url: "http://${LCP_HOST}:${LCP_PORT}/lcp/licenses/{license_id}"
license_status:
  register: true
  renew: true
  return: true
  renting_days: 60
  renew_days: 7
lcp:
  public_base_url:  "${LCP_PUBLIC_BASE_URL}"
lcp_update_auth:
  username: "${LCP_UPDATE_USER}"
  password: "${LCP_UPDATE_PASS}"
EOF

echo "[+] LCP config.yaml and lsd_config.yaml generated."
