#!/usr/bin/env bash

function install_cloudflared() {
    echo "[+] Installing cloudflared..."

    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install cloudflared
        OS="mac"
    elif [[ "$OSTYPE" == "linux-gnu"* ]] || [[ "$(uname -s)" == "Linux" ]]; then
        curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
        chmod +x cloudflared
        sudo mv cloudflared /usr/local/bin/
    else
        echo "[!] Please install cloudflared manually from https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/"
        exit 1
    fi
}

function get_tunnel() {
    grep -aEo 'https://[a-zA-Z0-9.-]+\.(trycloudflare|cfargotunnel)\.com' cloudflared.log | head -n1
}

function close_tunnel() {
    local url=$(get_tunnel)
    if [[ -n "$url" ]]; then
	pkill -f 'cloudflared tunnel --url'
	rm cloudflared.log
	echo "[+] Closing cloudflared tunnel $url"
    fi
}

function verify_tunnel() {
    local url="$1"
    if ! pgrep -f 'cloudflared tunnel --url' > /dev/null; then
        echo "[!] Cloudflared process is not running."
        return 1
    fi

    if ! curl -s --head --max-time 5 "$url" > /dev/null; then
         echo "[!] Tunnel URL $url is not reachable."
         return 1
    fi
    return 0
}

function create_tunnel() {
    ! command -v cloudflared &> /dev/null && install_cloudflared
    local port="${1:-8080}"  # default to 8080 if not provided
    local url=$(get_tunnel)

    # Check if we have an existing URL and if it's healthy
    if [[ -n "$url" ]]; then
        echo "[*] Found existing tunnel: $url. Checking health..."
        if verify_tunnel "$url"; then
            echo "[+] Existing tunnel is active and working."
            return 0
        else
            echo "[!] Existing tunnel is dead. Restarting..."
            pkill -f 'cloudflared tunnel --url' || true
            rm -f cloudflared.log
            url=""
        fi
    fi

    if [[ -z "$url" ]]; then
	echo "[+] Exposing local service on port $port via cloudflared..."
	nohup cloudflared tunnel --url http://localhost:"$port" --no-autoupdate > cloudflared.log 2>&1 &
    fi
	
    for i in {1..30}; do
        sleep 1
        # Try to extract any https://*.trycloudflare.com or https://*.cfargotunnel.com URL
	url=$(get_tunnel)
        if [[ -n "$url" ]]; then
	    echo "[+] Public cloudflared tunnel is running at: $url"
	    return 0
        fi
    done
    if [[ -z "$url" ]]; then
        echo "[!] Failed to get cloudflared public URL. Full cloudflared.log follows:"
        cat cloudflared.log
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  if [[ "$1" == "--start" ]]; then
    create_tunnel
  elif [[ "$1" == "--stop" ]]; then
    close_tunnel
  else    
    echo "Usage: $0 --start or --stop"
  fi
fi