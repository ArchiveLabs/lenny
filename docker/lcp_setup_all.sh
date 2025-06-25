#!/bin/sh
# Complete setup for Readium LCP server: config, htpasswd, certs, db
set -e

# 2. Generate config.yaml and lsd_config.yaml
sh docker/generate_lcp_configs.sh

# 3. Generate htpasswd
sh docker/generate_htpasswd.sh

# 4. Ensure cert files exist (copy test certs if missing)
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

# 5. Ensure DB schema files exist
# (No echo output needed)

echo "[+]LCP server config setup complete "
