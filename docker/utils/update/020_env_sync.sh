#!/usr/bin/env bash
set -euo pipefail

# Sync new environment variables from configure.sh into .env and reader.env
#
# Safety guarantees:
# - NEVER deletes any env file
# - NEVER overwrites existing values
# - NEVER removes user-added variables
# - Backs up env files before any modification
# - Only appends missing keys with safe defaults

CONFIGURE_SCRIPT="$LENNY_ROOT/docker/configure.sh"
BACKUP_DIR="$LENNY_ROOT/backups"

if [ ! -f "$CONFIGURE_SCRIPT" ]; then
    echo "configure.sh not found, skipping env sync."
    exit 0
fi

# ── Helper: sync one env file 
# Usage: sync_env_file <env_file> <heredoc_marker> <label>
#   env_file:       path to the .env file to sync
#   heredoc_marker: the variable name used in configure.sh (e.g., LENNY_ENV_FILE or READER_ENV_FILE)
#   label:          display label for log messages (e.g., ".env" or "reader.env")
sync_env_file() {
    local env_file="$1"
    local heredoc_marker="$2"
    local label="$3"

    if [ ! -f "$env_file" ]; then
        echo "  ${label}: file not found, skipping."
        return 0
    fi

    # Extract KEY=VALUE lines from the heredoc in configure.sh
    local template_vars
    template_vars=$(
        sed -n "/cat <<EOF > \"\$${heredoc_marker}\"/,/^EOF$/p" "$CONFIGURE_SCRIPT" \
        | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' \
        | cut -d= -f1
    ) || true

    if [ -z "$template_vars" ]; then
        echo "  ${label}: no template variables found, skipping."
        return 0
    fi

    # First pass: find missing variables
    local missing_vars=""
    while IFS= read -r var; do
        [ -z "$var" ] && continue
        if ! grep -qE "^${var}=" "$env_file"; then
            missing_vars="$missing_vars $var"
        fi
    done <<< "$template_vars"

    # Nothing to do
    if [ -z "$missing_vars" ]; then
        echo "  ${label}: all variables present."
        return 0
    fi

    # Back up before modifying
    mkdir -p "$BACKUP_DIR"
    local timestamp
    timestamp=$(date +%Y%m%d_%H%M%S)
    local backup_name
    backup_name=$(basename "$env_file")
    cp "$env_file" "$BACKUP_DIR/${backup_name}.${timestamp}.bak"
    echo "  ${label}: backed up → backups/${backup_name}.${timestamp}.bak"

    # Second pass: append missing variables
    local added=0
    for var in $missing_vars; do
        # Extract default value from the heredoc template
        local default_line
        default_line=$(
            sed -n "/cat <<EOF > \"\$${heredoc_marker}\"/,/^EOF$/p" "$CONFIGURE_SCRIPT" \
            | grep -E "^${var}=" \
            | head -1
        ) || true

        local value
        value=$(echo "$default_line" | cut -d= -f2-)

        # If the value is a shell variable reference ($VAR or ${VAR...}),
        # resolve the default from its assignment in configure.sh.
        # Generated values (passwords/keys using $(genpass)) stay empty.
        value=$(echo "$value" | sed 's/^[[:space:]]*//')
        if echo "$value" | grep -qE '^\$'; then
            local ref_var
            ref_var=$(echo "$value" | sed 's/^\${\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\).*/\1/')
            local default
            default=$(grep -E "^[[:space:]]*${ref_var}=\"\\\$\{${ref_var}:-[^}]*\}\"" "$CONFIGURE_SCRIPT" \
                | sed "s/.*:-\(.*\)}\".*/\1/" | head -1) || true
            if [ -n "$default" ] && ! echo "$default" | grep -qE '^\$\('; then
                value="$default"
            else
                value=""
            fi
        fi

        echo "${var}=${value}" >> "$env_file"
        echo "    Added: ${var}=${value}"
        added=$((added + 1))
    done

    echo "  ${label}: added $added new variable(s)."
}

# ── Sync both env files

echo "Syncing environment variables..."
sync_env_file "$LENNY_ROOT/.env"       "LENNY_ENV_FILE"  ".env"
sync_env_file "$LENNY_ROOT/reader.env" "READER_ENV_FILE" "reader.env"
