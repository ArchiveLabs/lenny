#!/usr/bin/env bash
set -e
echo "Welcome to Lenny Installer for Mac & Linux"

if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="linux"
elif [[ "$OSTYPE" == "darwin"* ]]; then
    OS="mac"
else
    echo "[!] Only Mac & Linux supported, detected: $OSTYPE"
    exit 1
fi

if [[ ! -d "lenny" ]]; then
  echo "[+] Downloading Lenny source code..."
  mkdir -p lenny
  curl -L https://github.com/ArchiveLabs/lenny/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 -C lenny
  echo "[✓] Downloaded Lenny source code..."
fi

wait_for_docker_ready() {
    echo "[+] Waiting up to 1 minute for Docker to start..."
    for i in {1..10}; do
	docker info >/dev/null 2>&1 && { echo "[+] Docker ready, beginning Lenny install."; break; }
	echo "Waiting for Docker ($i/10)..."
	sleep 6
	[[ $i -eq 10 ]] && { echo "Error: Docker not ready after 1 minute."; exit 1; }
    done
}

if ! command -v docker >/dev/null 2>&1; then
    echo "[+] Installing `docker` to build Lenny..."
    if [ "$OS" == "mac" ]; then	
	if ! command -v brew >/dev/null 2>&1; then
	    echo "[+] Installing Homebrew to get docker..."
	    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
	fi
	echo "[+] Installing Docker Desktop via Homebrew..."
	brew install --cask docker
	echo "[+] Loading docker..."
	open -a Docker
	echo "[+] Waiting for docker to start..."
	wait_for_docker_ready
    elif [ "$OS" == "linux" ]; then
	curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker "$USER"
	sudo systemctl start docker
	sudo systemctl enable docker
    fi
    wait_for_docker_ready
fi

cd lenny
./run.sh --preload




