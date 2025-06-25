#!/usr/bin/env bash
# Script to refresh both environment configuration and LCP configs

set -e

echo "🔄 Refreshing configuration files..."

# Force regenerate .env file
echo "📝 Regenerating .env file..."
./docker/configure.sh --force

# Generate fresh LCP configuration files
echo "🔧 Generating LCP configuration files..."
./docker/generate_lcp_configs.sh

# Generate htpasswd file if needed
if [ -f "./docker/generate_htpasswd.sh" ]; then
    echo "🔐 Generating htpasswd file..."
    ./docker/generate_htpasswd.sh
fi

echo "✅ All configuration files refreshed successfully!"
echo "🚀 You can now run: docker compose up -d"
