#!/usr/bin/env bash

wait_for_docker_ready() {
    echo "[+] Waiting up to 1 minute for Docker to start..."
    for i in {1..10}; do
	docker info >/dev/null 2>&1 && { echo "[+] Docker ready, beginning Lenny install."; break; }
	echo "Waiting for Docker ($i/10)..."
	sleep 6
	[[ $i -eq 10 ]] && { echo "Error: Docker not ready after 1 minute."; exit 1; }
    done
}

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
