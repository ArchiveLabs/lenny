#!/bin/sh
# Generates config.yaml and lsd_config.yaml for Readium LCP/LSD servers using environment variables from .env

set -e

ENV_FILE="./.env"
[ -f "$ENV_FILE" ] && export $(grep -v '^#' "$ENV_FILE" | xargs)

CONFIG_DIR="./readium/config"

cat > "$CONFIG_DIR/config.yaml" <<EOF
profile: "basic"
lcp:
    host: "${LCP_HOST}"
    port: ${LCP_PORT}
    public_base_url: "${LCP_PUBLIC_BASE_URL}"
    database: "postgres://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${LCP_DB_NAME}?sslmode=disable"
    auth_file: "/readium/config/htpasswd"
certificate:
    cert: "/readium/config/cert-edrlab-test.pem"
    private_key: "/readium/config/privkey-edrlab-test.pem"
license:
    links:
        status: "http://${LCD_HOST}:${LCD_PORT}/lcp/licenses/{license_id}/status"
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
  database: "postgres://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${LCP_DB_NAME}?sslmode=disable"
  auth_file: "/readium/config/htpasswd"
  license_link_url: "http://${LENNY_HOST}:${LENNY_PORT}/lcp/licenses/{license_id}"
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

echo "[+]LCP config.yaml and lsd_config.yaml generated."
