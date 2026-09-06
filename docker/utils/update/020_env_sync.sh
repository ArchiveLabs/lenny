#!/usr/bin/env bash
set -euo pipefail

genpass() {
    local len=${1:-32}
    dd if=/dev/urandom bs=1 count=$((len * 2)) 2>/dev/null | base64 | tr -dc 'A-Za-z0-9' | head -c "$len"
}

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

# ── Bootstrap missing split env files ─────────────────────────────────────────
#
# When a new release introduces a new env file (e.g., auth.env, ol.env),
# existing installs won't have it on disk. Bootstrap by sourcing the legacy .env
# first — so any existing values flow into shell env vars — then running
# configure.sh, which:
#   * SKIPS files that already exist (never clobbers user config)
#   * For files it creates, uses ${KEY:-$(genpass …)} so existing .env values
#     are reused and only genuinely missing secrets are generated fresh.
#
# This preserves credential integrity across the .env → split-file migration.
bootstrap_missing_env_files() {
    local lenny_env="$LENNY_ROOT/.env"
    [ -f "$lenny_env" ] || return 0

    # Skip work when every split env file already exists — keeps the no-op
    # update path silent and avoids invoking configure.sh unnecessarily.
    local need_bootstrap=0
    for f in auth.env ol.env reader.env loan.env; do
        if [ ! -f "$LENNY_ROOT/$f" ]; then
            need_bootstrap=1
            break
        fi
    done
    if [ "$need_bootstrap" -eq 0 ]; then
        return 0
    fi

    echo "  Bootstrapping missing split env files via configure.sh..."

    # Source .env in a subshell with `set -a` so KEY=VALUE lines export, then
    # invoke configure.sh — its `${KEY:-default}` patterns will pick up the
    # exported values and reuse them instead of generating fresh secrets.
    #
    # SECURITY NOTE: `source .env` will execute any shell expansions present in
    # values (backticks, $(…)). configure.sh-generated .env files only contain
    # literal KEY=VALUE pairs, so this is safe in practice; but a user-modified
    # .env with command substitution would be evaluated. .env is chmod 600;
    # write access already implies code-execution capability.
    (
        set -a
        # shellcheck disable=SC1090
        . "$lenny_env"
        set +a
        cd "$LENNY_ROOT" && bash "$CONFIGURE_SCRIPT"
    ) || {
        echo "  WARNING: configure.sh bootstrap failed; continuing with sync." >&2
        return 0
    }
}

bootstrap_missing_env_files

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

    # A file the API container wrote (e.g. via /admin/loan/settings) can end
    # up root-owned on the host, since the container runs as root — don't
    # let one unreadable file abort the whole update.
    if [ ! -r "$env_file" ]; then
        echo "  ${label}: not readable (check ownership/permissions), skipping." >&2
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
        # Generated values (passwords/keys using $(genpass)) are auto-generated.
        value=$(echo "$value" | sed 's/^[[:space:]]*//')
        if echo "$value" | grep -qE '^\$'; then
            local ref_var
            ref_var=$(echo "$value" | sed 's/^\${\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\).*/\1/')
            local default
            default=$(grep -E "^[[:space:]]*${ref_var}=\"\\\$\{${ref_var}:-[^}]*\}\"" "$CONFIGURE_SCRIPT" \
                | sed "s/.*:-\(.*\)}\".*/\1/" | head -1) || true
            if [ -n "$default" ] && ! echo "$default" | grep -qE '^\$\('; then
                value="$default"
            elif echo "$default" | grep -qE '^\$\(genpass'; then
                local genpass_len
                genpass_len=$(echo "$default" | grep -oE '[0-9]+' | head -1)
                value=$(genpass "${genpass_len:-32}")
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

# ── Generic .env key migration (idempotent, additive-only) ──────────────────
#
# Copies a set of keys from .env into a target env file (auth.env, ol.env, …)
# so credentials originally created in .env survive the move to split files.
#
# Guarantees:
#   * Backs up both .env and the target before touching either.
#   * Never overwrites an existing value in the target.
#   * Never removes a key from .env — originals stay in place. Compose loads
#     the split file AFTER .env in env_file ordering, so the target wins at
#     runtime. .env keeps its copy purely as a paper trail / fallback.
#   * Idempotent: re-running with all keys already present in target is a no-op.
#
# Rotation note (read this before rotating secrets):
#   If you rotate a secret in auth.env (or other split file), the OLD value
#   still lingers in .env. The backend will use the rotated value (split file
#   wins), but the stale value remains on disk until you remove it manually.
#   A warning is printed below to remind you.
#
# Usage: migrate_keys "<SPACE_SEPARATED_KEYS>" "<target_env_path>" "<label>"

_ADMIN_MIGRATE_KEYS="ADMIN_USERNAME ADMIN_PASSWORD ADMIN_INTERNAL_SECRET ADMIN_SALT"
_OL_MIGRATE_KEYS="OL_S3_ACCESS_KEY OL_S3_SECRET_KEY OL_USERNAME LENNY_LENDING_ENABLED LENNY_LENDING_MODE LENNY_OL_INDEXED"

migrate_keys() {
    local keys="$1"
    local target_env="$2"
    local label="$3"
    local lenny_env="$LENNY_ROOT/.env"
    local _tmp=""

    if [ ! -f "$target_env" ] || [ ! -f "$lenny_env" ]; then
        return 0
    fi

    # See sync_env_file's comment — a container-written file can be
    # root-owned on the host; don't let that abort the whole update.
    if [ ! -r "$target_env" ] || [ ! -w "$target_env" ]; then
        echo "  ${label}: ${target_env} not readable/writable (check ownership/permissions), skipping." >&2
        return 0
    fi

    local need_migrate=0
    for key in $keys; do
        if grep -qE "^${key}=" "$lenny_env"; then
            need_migrate=1
            break
        fi
    done

    if [ "$need_migrate" -eq 0 ]; then
        return 0
    fi

    # Install trap only after all early returns — avoids leaving a stale
    # process-level EXIT handler when returning early from a no-op call.
    trap '[ -n "$_tmp" ] && rm -f "$_tmp"' EXIT

    local target_name
    target_name=$(basename "$target_env")
    echo "  Migrating $label from .env → ${target_name}..."

    mkdir -p "$BACKUP_DIR"
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    cp "$lenny_env"   "$BACKUP_DIR/.env.${ts}.bak"
    cp "$target_env"  "$BACKUP_DIR/${target_name}.${ts}.bak"
    echo "    Backed up .env and ${target_name} → backups/"

    local copied=0
    for key in $keys; do
        local line
        line=$(grep -E "^${key}=" "$lenny_env" | head -1) || true
        if [ -z "$line" ]; then
            continue
        fi
        local value="${line#*=}"

        if grep -qE "^${key}=" "$target_env"; then
            # Target already has it — preserve target, do nothing.
            continue
        fi

        echo "${key}=${value}" >> "$target_env"
        echo "    Copied: ${key} → ${target_name} (kept in .env as backup)"
        copied=$((copied + 1))
    done

    if [ "$copied" -gt 0 ]; then
        echo "    Note: ${copied} key(s) now live in BOTH .env and ${target_name}."
        echo "    ${target_name} takes precedence at runtime. If you rotate"
        echo "    secrets in ${target_name}, remove the stale copies in .env manually."
    fi

    trap - EXIT
}

# ── One-time: migrate LENNY_LENDING_ENABLED → LENNY_LENDING_MODE ─────────────
#
# LENNY_LENDING_ENABLED (bool true/false) was replaced by the 3-state
# LENNY_LENDING_MODE (none | ol | external).  This block runs once per
# deployment: if the old key is still in ol.env it is converted and removed.
#
migrate_lending_mode() {
    local ol_env="$LENNY_ROOT/ol.env"
    local _tmp=""

    # Early returns before any file is touched — no trap needed yet.
    [ -f "$ol_env" ] || return 0
    # See sync_env_file's comment — a container-written file can be
    # root-owned on the host; don't let that abort the whole update.
    if [ ! -r "$ol_env" ] || [ ! -w "$ol_env" ]; then
        echo "  ol.env: not readable/writable (check ownership/permissions), skipping." >&2
        return 0
    fi
    grep -qE "^LENNY_LENDING_ENABLED=" "$ol_env" || return 0

    # From here we will write — install cleanup trap.
    trap '[ -n "$_tmp" ] && rm -f "$_tmp"' EXIT

    # Already migrated — new key present, just remove the obsolete one.
    if grep -qE "^LENNY_LENDING_MODE=" "$ol_env"; then
        mkdir -p "$BACKUP_DIR"
        local ts
        ts=$(date +%Y%m%d_%H%M%S)
        cp "$ol_env" "$BACKUP_DIR/ol.env.${ts}.bak"
        _tmp=$(mktemp "${ol_env}.XXXXXX")
        local perms
        perms=$(stat -c "%a" "$ol_env" 2>/dev/null || stat -f "%OLp" "$ol_env" 2>/dev/null || echo "600")
        chmod "$perms" "$_tmp" 2>/dev/null || chmod 600 "$_tmp"
        grep -vE "^LENNY_LENDING_ENABLED=" "$ol_env" > "$_tmp" || true
        mv "$_tmp" "$ol_env"
        _tmp=""
        echo "  ol.env: removed obsolete LENNY_LENDING_ENABLED (LENNY_LENDING_MODE already set)."
        trap - EXIT
        return 0
    fi

    # Derive new mode from old boolean value
    local old_val
    old_val=$(grep -E "^LENNY_LENDING_ENABLED=" "$ol_env" | head -1 | cut -d= -f2-)
    local new_mode="none"
    [ "$old_val" = "true" ] && new_mode="ol"

    mkdir -p "$BACKUP_DIR"
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    cp "$ol_env" "$BACKUP_DIR/ol.env.${ts}.bak"
    echo "  ol.env: migrating LENNY_LENDING_ENABLED=$old_val → LENNY_LENDING_MODE=$new_mode"
    echo "    Backed up → backups/ol.env.${ts}.bak"

    # Write new key and remove old key atomically
    _tmp=$(mktemp "${ol_env}.XXXXXX")
    local perms
    perms=$(stat -c "%a" "$ol_env" 2>/dev/null || stat -f "%OLp" "$ol_env" 2>/dev/null || echo "600")
    chmod "$perms" "$_tmp" 2>/dev/null || chmod 600 "$_tmp"
    {
        grep -vE "^LENNY_LENDING_ENABLED=" "$ol_env" || true
        echo "LENNY_LENDING_MODE=$new_mode"
    } > "$_tmp"
    mv "$_tmp" "$ol_env"
    _tmp=""

    echo "  ol.env: migration complete."
    trap - EXIT
}

# ── One-time: move loan settings from .env → loan.env ────────────────────────
#
# Loan policy (LENNY_LOAN_LIMIT, LENNY_LOAN_DURATION_DAYS) used to live in .env.
# It is now runtime-editable via /admin/loan/settings, so it gets its own file.
# Unlike migrate_keys (additive, keeps a copy in .env), this does a CLEAN MOVE:
# copy into loan.env, then remove from .env — a single source of truth, no stale
# dupes. Loan settings are not secrets, so relocation is safe. Idempotent.
#
_LOAN_MIGRATE_KEYS="LENNY_LOAN_LIMIT LENNY_LOAN_DURATION_DAYS"

migrate_loan_settings() {
    local lenny_env="$LENNY_ROOT/.env"
    local loan_env="$LENNY_ROOT/loan.env"
    local _tmp=""

    # Early returns before any file is touched — no trap needed yet.
    [ -f "$lenny_env" ] || return 0
    [ -f "$loan_env" ]  || return 0
    # See sync_env_file's comment — a container-written file can be
    # root-owned on the host; don't let that abort the whole update.
    if [ ! -r "$loan_env" ] || [ ! -w "$loan_env" ]; then
        echo "  loan.env: not readable/writable (check ownership/permissions), skipping." >&2
        return 0
    fi

    local present=0
    for key in $_LOAN_MIGRATE_KEYS; do
        if grep -qE "^${key}=" "$lenny_env"; then
            present=1
            break
        fi
    done
    [ "$present" -eq 1 ] || return 0

    # From here we will write — install cleanup trap.
    trap '[ -n "$_tmp" ] && rm -f "$_tmp"' EXIT

    echo "  Migrating loan settings from .env → loan.env..."
    mkdir -p "$BACKUP_DIR"
    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    cp "$lenny_env" "$BACKUP_DIR/.env.${ts}.bak"
    cp "$loan_env"  "$BACKUP_DIR/loan.env.${ts}.bak"
    echo "    Backed up .env and loan.env → backups/"

    # Copy each key into loan.env if not already present there.
    for key in $_LOAN_MIGRATE_KEYS; do
        grep -qE "^${key}=" "$loan_env" && continue
        local line
        line=$(grep -E "^${key}=" "$lenny_env" | head -1) || true
        [ -n "$line" ] && echo "$line" >> "$loan_env" && echo "    Copied: ${key} → loan.env"
    done

    # Atomically rewrite .env with the loan keys stripped out.
    _tmp=$(mktemp "${lenny_env}.XXXXXX")
    local perms
    perms=$(stat -c "%a" "$lenny_env" 2>/dev/null || stat -f "%OLp" "$lenny_env" 2>/dev/null || echo "600")
    chmod "$perms" "$_tmp" 2>/dev/null || chmod 600 "$_tmp"
    grep -vE "^(LENNY_LOAN_LIMIT|LENNY_LOAN_DURATION_DAYS)=" "$lenny_env" > "$_tmp" || true
    mv "$_tmp" "$lenny_env"
    _tmp=""

    echo "    Removed loan keys from .env (now live in loan.env only)."
    echo "  loan settings migration complete."
    trap - EXIT
}

# ── Sync all env files ────────────────────────────────────────────────────────

echo "Syncing environment variables..."
migrate_lending_mode
migrate_loan_settings
migrate_keys "$_ADMIN_MIGRATE_KEYS" "$LENNY_ROOT/auth.env" "admin credentials"
migrate_keys "$_OL_MIGRATE_KEYS"    "$LENNY_ROOT/ol.env"   "OL/IA credentials"
sync_env_file "$LENNY_ROOT/.env"         "LENNY_ENV_FILE"  ".env"
sync_env_file "$LENNY_ROOT/reader.env"   "READER_ENV_FILE" "reader.env"
sync_env_file "$LENNY_ROOT/auth.env"     "AUTH_ENV_FILE"   "auth.env"
sync_env_file "$LENNY_ROOT/ol.env"       "OL_ENV_FILE"     "ol.env"
sync_env_file "$LENNY_ROOT/loan.env"     "LOAN_ENV_FILE"   "loan.env"
