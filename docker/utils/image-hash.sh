#!/usr/bin/env bash
set -euo pipefail

# ── Lenny image content-hash ─────────────────────────────────────────────────
# Prints a stable digest of ONE prebaked image's declared build inputs, or the
# full ghcr reference carrying that digest as its tag.
#
# This is the single source of truth for "does this image need rebuilding?".
# BOTH sides call it so they can never drift:
#   - CI (.github/workflows/docker-publish.yml) tags each published image with
#     `inputs-<hash>` and skips the build when that tag already exists in ghcr.
#   - The consume side (docker/utils/update/030_pull_images.sh) resolves the same
#     `inputs-<hash>` and pulls that exact tag, falling back to a local build on
#     a miss.
#
# A `paths:` filter is a fast path that can be subtly wrong and ship a stale
# image; this hash cannot. It is a pure function of each image's *declared*
# inputs, so the tag changes exactly when a rebuild is actually required.
#
# Usage:
#   image-hash.sh tag   <service>   # -> inputs-<16 hex>
#   image-hash.sh ref   <service>   # -> ghcr.io/archivelabs/lenny-<service>:inputs-<hex>
#   image-hash.sh image <service>   # -> ghcr.io/archivelabs/lenny-<service>
#   image-hash.sh hash  <service>   # -> <16 hex> (bare digest, no prefix)
#
# <service> is one of: api reader admin opds
#
# Inputs per service (why each set is exact — see ArchiveLabs/lenny#144):
#   api    COPYs from the build context, so its hash must cover that whole set:
#          its Dockerfile, requirements.txt, the two nginx confs it bakes in,
#          migrate.sh, VERSION, alembic.ini, alembic/** and lenny/**.
#   reader no COPY from context — it clones a pinned THORIUM_REF and builds. The
#          ref is a default ARG *inside* the Dockerfile, so the Dockerfile's
#          bytes already capture it. MANIFEST_ALLOWED_DOMAINS is read at runtime
#          (not inlined) and MANIFEST_ROUTE_FORCE_ENABLE is a project-wide
#          constant, so neither belongs in the hash — the shared image is built
#          once and those are overridden per deployment. Hash = Dockerfile only.
#   admin  no COPY from context (clones a pinned LENNY_APP_REF, also an in-file
#          ARG). But NEXT_PUBLIC_API_URL IS inlined into the client bundle at
#          build time, and configure.sh writes it per install — so it is folded
#          into the hash. An install with a custom value gets a cache miss and a
#          local build, never a silently-wrong shared image.
#   opds   COPYs lock-to-lenny.py and nginx.conf; clones a pinned OPDS_CLIENT_REF
#          (in-file ARG). Hash = Dockerfile + those two files.

REGISTRY="${LENNY_REGISTRY:-ghcr.io}"
ORG="${LENNY_IMAGE_ORG:-archivelabs}"

usage() {
    echo "usage: $(basename "$0") {tag|ref|image|hash} {api|reader|admin|opds}" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage
mode="$1"
service="$2"

# Resolve repo root so the script works from any cwd (CI, worktree, server).
ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | cut -d' ' -f1
    else
        # macOS / BSD
        shasum -a 256 | cut -d' ' -f1
    fi
}

# Declared input paths (files and/or directories) for a service, relative to
# ROOT. Directories are expanded to their git-tracked members.
inputs_for() {
    case "$1" in
        api)
            echo "docker/api/Dockerfile"
            echo "requirements.txt"
            echo "docker/nginx/nginx.conf"
            echo "docker/nginx/conf.d/lenny.conf"
            echo "docker/utils/migrate.sh"
            echo "VERSION"
            echo "alembic.ini"
            echo "alembic"
            echo "lenny"
            ;;
        reader)
            echo "docker/reader/Dockerfile"
            ;;
        admin)
            echo "docker/lenny-app/Dockerfile"
            ;;
        opds)
            echo "docker/opds-client/Dockerfile"
            echo "docker/opds-client/lock-to-lenny.py"
            echo "docker/opds-client/nginx.conf"
            ;;
        *)
            echo "unknown service: $1" >&2
            exit 2
            ;;
    esac
}

# Build-arg values that are genuinely baked into a service's image and can vary
# per install. Emitted into the hash stream as `ARG name=value` lines so a
# custom value produces a distinct hash (cache miss -> local build).
baked_args_for() {
    case "$1" in
        admin)
            # Inlined into the client bundle; configure.sh writes it per install.
            printf 'ARG NEXT_PUBLIC_API_URL=%s\n' "${NEXT_PUBLIC_API_URL:-}"
            ;;
        *)
            : # api/reader/opds bake nothing that varies per install.
            ;;
    esac
}

compute_hash() {
    local svc="$1"
    cd "$ROOT"
    {
        # Content of every declared input, in a stable order. `git hash-object`
        # digests the working-tree bytes Docker would COPY/build; pairing it
        # with the path means a rename changes the hash too. Directories are
        # enumerated via tracked files only — deterministic, and a clean
        # checkout (which `make update` enforces) has no untracked strays.
        while IFS= read -r path; do
            [ -n "$path" ] || continue
            if [ -d "$path" ]; then
                git ls-files -z -- "$path"
            else
                printf '%s\0' "$path"
            fi
        done < <(inputs_for "$svc") \
            | LC_ALL=C sort -z -u \
            | while IFS= read -r -d '' f; do
                printf '%s  %s\n' "$(git hash-object "$f")" "$f"
            done
        # Per-install baked build args.
        baked_args_for "$svc"
    } | sha256 | cut -c1-16
}

hash="$(compute_hash "$service")"

case "$mode" in
    hash)  printf '%s\n' "$hash" ;;
    tag)   printf 'inputs-%s\n' "$hash" ;;
    image) printf '%s/%s/lenny-%s\n' "$REGISTRY" "$ORG" "$service" ;;
    ref)   printf '%s/%s/lenny-%s:inputs-%s\n' "$REGISTRY" "$ORG" "$service" "$hash" ;;
    *)     usage ;;
esac
