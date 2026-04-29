#!/bin/sh
set -e
echo "Welcome to Lenny Installer for Mac & Linux"

# ─── Argument & environment parsing ──────────────────────────────────
# -y / --yes / LENNY_DEFAULTS=1 skips all prompts and accepts all defaults
# (no preload, no lending, no OL indexing — matches `ia --configure` opt-in
# ethos). Set LENNY_PRELOAD=1, LENNY_LENDING=1, LENNY_INDEXED=1 individually
# to override any default from the environment.
LENNY_DEFAULTS="${LENNY_DEFAULTS:-0}"
for arg in "$@"; do
    case "$arg" in
        -y|--yes) LENNY_DEFAULTS=1 ;;
    esac
done

if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    OS="linux"
elif [[ "$OSTYPE" == "darwin"* ]]; then
    OS="mac"
else
    echo "[!] Only Mac & Linux supported, detected: $OSTYPE"
    exit 1
fi

if [ "$OS" = "linux" ]; then
  echo "[+] Updating package index (apt)..."
  sudo apt update -y

  if ! require make; then
    echo "[+] Installing build-essential (make, gcc, etc.)..."
    sudo apt install -y build-essential bc
  fi

  if ! require curl; then
    echo "[+] Installing curl..."
    sudo apt install -y curl
  fi
fi

if [[ ! -d "lenny" ]]; then
  echo "[+] Downloading Lenny source code..."
  mkdir -p lenny
  curl -L https://github.com/ArchiveLabs/lenny/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 -C lenny
  echo "[✓] Downloaded Lenny source code..."
fi

# TODO: Switch to docker/utils/docker_helpers
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

# ─── Install prompts ──────────────────────────────────────────────────
# Ask three yes/no questions (preload / lending / OL indexing). `-y` or
# LENNY_DEFAULTS=1 skips prompts and answers "no" to all. Individual
# env overrides (LENNY_PRELOAD, LENNY_LENDING, LENNY_INDEXED) take
# precedence over both the default AND the prompt.
#
# Reads from /dev/tty so piped installs (`curl | sh`) that land at a
# TTY still work. When no TTY is available and LENNY_DEFAULTS is not
# set, we fall back to "no" rather than blocking the install.
ask_yes_no() {
    # $1: prompt, $2: default (y|n)
    local prompt="$1" default="$2" reply
    if [ "$LENNY_DEFAULTS" = "1" ]; then
        reply="$default"
    elif [ -r /dev/tty ]; then
        if [ "$default" = "y" ]; then
            printf '[?] %s [Y/n] ' "$prompt" >/dev/tty
        else
            printf '[?] %s [y/N] ' "$prompt" >/dev/tty
        fi
        IFS= read -r reply </dev/tty || reply="$default"
        reply="${reply:-$default}"
        reply="$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')"
        case "$reply" in y|yes) reply=y ;; *) reply=n ;; esac
    else
        echo "[!] No TTY available — defaulting '${prompt}' to '${default}'. Re-run with -y to silence this."
        reply="$default"
    fi
    [ "$reply" = "y" ]
}

# Resolve each answer — honour explicit env overrides first.
if [ -n "${LENNY_PRELOAD:-}" ]; then
    [ "$LENNY_PRELOAD" = "1" ] && PRELOAD=1 || PRELOAD=0
elif ask_yes_no "Preload standard ebooks?" "n"; then
    PRELOAD=1
else
    PRELOAD=0
fi

if [ -n "${LENNY_LENDING:-}" ]; then
    [ "$LENNY_LENDING" = "1" ] && LENDING=1 || LENDING=0
elif ask_yes_no "Enable lending (use openlibrary.org for OTP auth)?" "n"; then
    LENDING=1
else
    LENDING=0
fi

if [ -n "${LENNY_INDEXED:-}" ]; then
    [ "$LENNY_INDEXED" = "1" ] && INDEXED=1 || INDEXED=0
elif ask_yes_no "Index your borrowable books in Open Library?" "n"; then
    INDEXED=1
else
    INDEXED=0
fi

# These env vars flow through to configure.sh's heredoc so they end up in .env.
if [ "$LENDING" = "1" ]; then LENDING_ENV=true; else LENDING_ENV=false; fi
if [ "$INDEXED" = "1" ]; then INDEXED_ENV=true; else INDEXED_ENV=false; fi
export LENNY_LENDING_ENABLED="$LENDING_ENV"
export LENNY_OL_INDEXED="$INDEXED_ENV"

cd lenny

# Preserve the env vars through sudo so configure.sh picks them up.
sudo -E env LENNY_LENDING_ENABLED="$LENNY_LENDING_ENABLED" LENNY_OL_INDEXED="$LENNY_OL_INDEXED" \
     make tunnel configure rebuild

# ─── Post-rebuild: Open Library auth (if lending enabled) ────────────
# The ol_configure script authenticates against archive.org, writes the
# returned IA S3 keys into .env, and restarts lenny_api so they're picked
# up. It's idempotent and supports re-running via `make ol-login`.
if [ "$LENDING" = "1" ]; then
    echo "[+] Lending enabled — configuring Open Library authentication..."
    if [ "$LENNY_DEFAULTS" = "1" ]; then
        echo "[!] Lending was enabled via LENNY_LENDING=1 but -y / LENNY_DEFAULTS=1 suppresses"
        echo "    interactive prompts. Run 'make ol-login' after installation to log in."
    else
        sudo bash docker/utils/ol_configure.sh || {
            echo "[!] Open Library login failed or was cancelled."
            echo "    Lenny is still installed — run 'make ol-login' to retry."
        }
    fi
fi

if [ "$PRELOAD" = "1" ]; then
    echo "[+] Starting preload step (with allocated TTY)..."
    sudo script -q -c "make preload" /dev/null
else
    echo "[+] Skipping preload (not requested). Run 'make preload' later if you change your mind."
fi

echo "[✓] Lenny installation complete!"
