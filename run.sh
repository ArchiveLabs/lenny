#!/usr/bin/env bash

./docker/configure.sh

MODE=
LOG=
REBUILD=false
PRELOAD=""
PUBLIC=false

# Parse cli args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --rebuild)
      REBUILD=true
      shift
      ;;
    --preload)
      # If next arg exists and is a value (not another flag)
      if [[ -n "$2" && "$2" != --* ]]; then
        PRELOAD="$2"
        shift 2
      else
        PRELOAD=true
        shift
      fi
      ;;
    --log)
      LOG=true
      shift
      ;;
    --public)
      PUBLIC=true
      shift
      ;;
    *)
      echo "Unknown argument: $1"
      shift
      ;;
  esac
done

# Function to wait for a Docker container to be running
# Arguments:
#   $1: The name of the Docker container to wait for (e.g., "lenny_api")
#   $2: (Optional) Maximum number of attempts (default: 15)
#   $3: (Optional) Sleep duration between attempts in seconds (default: 2)
function wait_for_docker_container() {
    local container_name="${1}"
    local max_attempts="${2:-15}" # Default to 15 attempts if not provided
    local wait_seconds="${3:-2}" # Default to 2 seconds if not provided

    if [[ -z "$container_name" ]]; then
        echo "[!] Error: No container name provided to wait_for_docker_container function."
        return 1 # Indicate failure
    fi

    echo "[+] Waiting up to $((max_attempts * wait_seconds)) seconds for '$container_name' to start and pass health checks..."

    local container_ready=false
    for ((i=1; i<=max_attempts; i++)); do
        if docker ps -f "name=$container_name" -f status=running -q &>/dev/null; then
            echo "[+] '$container_name' service is running."
            container_ready=true
            break # Exit the loop immediately
        fi

        echo "    Attempt $i/$max_attempts: Still waiting for '$container_name'..."
        sleep "$wait_seconds"
    done

    if ! "$container_ready"; then
        echo "[!] Error: '$container_name' did not launch after $((max_attempts * wait_seconds)) seconds."
        return 1 # Indicate failure
    fi

    return 0 # Indicate success
}

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

function create_tunnel() {
    ! command -v cloudflared &> /dev/null && install_cloudflared
    PORT="${LENNY_PORT:-8080}"
    echo "[+] Exposing local service on port $PORT via cloudflared..."
    cloudflared tunnel --url http://localhost:"$PORT" --no-autoupdate > cloudflared.log 2>&1 &
    CF_PID=$!

    # Trap to ensure cloudflared is killed if script exits or interrupted
    trap 'echo "[+] Cleaning up cloudflared..."; kill $CF_PID 2>/dev/null' EXIT

    for i in {1..30}; do
        sleep 1
        # Try to extract any https://*.trycloudflare.com or https://*.cfargotunnel.com URL
        URL=$(grep -Eo 'https://[a-zA-Z0-9.-]+\.(trycloudflare|cfargotunnel)\.com' cloudflared.log | head -n1)
        if [[ -n "$URL" ]]; then
            echo "[+] Your public URL is: $URL/v1/api/"
	    read -p "[+] Setting as LENNY_PROXY. Press Enter to continue..."
	    export LENNY_PROXY=$URL
            return 0
        fi
    done
    if [[ -z "$URL" ]]; then
        echo "[!] Failed to get cloudflared public URL. Full cloudflared.log follows:"
        cat cloudflared.log
    fi
}

echo "[+] Loading .env file"
export $(grep -v '^#' .env | xargs)

if [[ "$PUBLIC" == "true" ]]; then
    create_tunnel    
fi

if [[ "$REBUILD" == "true" ]]; then
    echo "[+] Performing full rebuild..."
    docker compose down --volumes --remove-orphans
    docker compose build --no-cache
    LENNY_PROXY="$LENNY_PROXY" docker compose up -d
else
    LENNY_PROXY="$LENNY_PROXY" docker compose -p lenny up -d
fi

if [[ -n "$PRELOAD" ]]; then
    if wait_for_docker_container "lenny_api" 15 2; then
        if [[ "$PRELOAD" =~ ^[0-9]+$ ]]; then
            EST_MIN=$(echo "scale=2; (800 / $PRELOAD) / 60" | bc)
            LIMIT="-n $PRELOAD"
        else
            EST_MIN=$(echo "scale=2; 800 / 60" | bc)
            LIMIT=""
        fi

        echo "[+] Preloading ${PRELOAD:-ALL}/~800 book(s) from StandardEbooks (~$EST_MIN minutes)..."
        docker exec -it lenny_api python scripts/preload.py $LIMIT
    fi
fi

if [[ "$LOG" == "true" ]]; then
    docker compose logs -f
elif [[ "$PUBLIC" == "true" ]]; then
    read -p "[+] Press Enter to close tunnel..."
fi
