#!/bin/sh
set -e
echo "Welcome to Lenny Installer for Mac & Linux"

OS_NAME="$(uname -s)"
case "$OS_NAME" in
  Linux*) OS="linux" ;;
  Darwin*) OS="mac" ;;
  *) echo "[!] Only Mac & Linux supported, detected: $OS_NAME"; exit 1 ;;
esac

if [ "$OS" = "linux" ]; then
  echo "[+] Updating package index (apt)..."
  sudo apt update -y

  if ! command -v make >/dev/null 2>&1; then
    echo "[+] Installing build-essential (make, gcc, etc.)..."
    sudo apt install -y build-essential bc
  fi

  if ! command -v curl >/dev/null 2>&1; then
    echo "[+] Installing curl..."
    sudo apt install -y curl
  fi
fi

if [ ! -d "lenny" ]; then
  echo "[+] Downloading Lenny source code..."
  mkdir -p lenny
  curl -L https://github.com/ArchiveLabs/lenny/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 -C lenny
  echo "[✓] Downloaded Lenny source code..."
fi

# TODO: Switch to docker/utils/docker_helpers
wait_for_docker_ready() {
  echo "[+] Waiting up to 1 minute for Docker to start..."
  i=1
  while [ $i -le 10 ]; do
    docker info >/dev/null 2>&1 && { echo "[+] Docker ready, beginning Lenny install."; break; }
    echo "Waiting for Docker ($i/10)..."
    sleep 6
    i=$((i + 1))
    if [ $i -gt 10 ]; then
      echo "Error: Docker not ready after 1 minute."
      exit 1
    fi
  done
}

if ! command -v docker >/dev/null 2>&1; then
  echo "[+] Installing docker to build Lenny..."
  if [ "$OS" = "mac" ]; then
    if ! command -v brew >/dev/null 2>&1; then
      echo "[+] Installing Homebrew to get docker..."
      /bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    echo "[+] Installing Docker Desktop via Homebrew..."
    brew install --cask docker
    echo "[+] Loading docker..."
    open -a Docker
    echo "[+] Waiting for docker to start..."    
    wait_for_docker_ready
  elif [ "$OS" = "linux" ]; then
    curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker "$USER"
    sudo systemctl start docker
    sudo systemctl enable docker
    wait_for_docker_ready
  fi
fi

cd lenny

sudo make tunnel configure rebuild

echo "[+] Starting preload step (with allocated TTY)..."
sudo script -q -c "make preload" /dev/null

echo "[✓] Lenny installation complete!"
