#!/usr/bin/env bash

echo "========================================="
echo "   Lenny OAuth Client Registration"
echo "========================================="
echo ""

read -p "Enter Client ID (leave blank to auto-generate): " CLIENT_ID
if [ -z "$CLIENT_ID" ]; then
    CLIENT_ID="client_$(openssl rand -hex 6)"
    echo "Generated Client ID: $CLIENT_ID"
fi

echo ""
echo "Enter allowed Redirect URIs (comma separated)."
echo "Example: opds://callback,http://localhost:3000/callback"
read -p "Redirect URIs: " REDIRECT_URIS

if [ -z "$REDIRECT_URIS" ]; then
    echo "Error: At least one Redirect URI is required."
    exit 1
fi

echo ""
echo "Registering client..."
docker compose exec api python3 scripts/register_client.py --client-id "$CLIENT_ID" --redirect-uris "$REDIRECT_URIS"

if [ $? -eq 0 ]; then
    echo ""
    echo "✅ Registration done."
    echo "   Client ID:      $CLIENT_ID"
    echo "   Redirect URIs:  $REDIRECT_URIS"
fi
