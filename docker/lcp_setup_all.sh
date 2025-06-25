#!/bin/sh
set -e

sh docker/generate_lcp_configs.sh
sh docker/generate_htpasswd.sh

CERT_DIR="./readium/config"
CERT_FILE="$CERT_DIR/cert-edrlab-test.pem"
KEY_FILE="$CERT_DIR/privkey-edrlab-test.pem"
TEST_CERT="./readium/config/test/cert/cert-edrlab-test.pem"
TEST_KEY="./readium/config/test/cert/privkey-edrlab-test.pem"
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    if [ -f "$TEST_CERT" ] && [ -f "$TEST_KEY" ]; then
        cp "$TEST_CERT" "$CERT_FILE"
        cp "$TEST_KEY" "$KEY_FILE"
    fi
fi

echo "[+]LCP server config setup complete "
