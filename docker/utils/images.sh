#!/usr/bin/env bash
# ── Lenny prebaked-image helpers ─────────────────────────────────────────────
# Sourced by the consume side (lenny.sh, the update pipeline) to resolve and
# pull the images CI prebakes, so a server never runs the expensive builds
# (above all the reader's full Next.js `pnpm build`) itself.
#
# The content-hash tag each image carries is computed by image-hash.sh — the
# single source of truth shared with CI, so the two can never drift.

LENNY_IMAGE_SERVICES="api reader admin opds"

_images_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# svc -> LENNY_<SVC>_TAG  (the compose variable that selects the image tag)
lenny_svc_tag_var() {
    printf 'LENNY_%s_TAG' "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')"
}

# Resolve the content-hash tag for every prebaked service and export it as the
# compose variable compose.yaml reads. After this, `${LENNY_<SVC>_TAG}` selects
# the exact image whose inputs match the local tree — not `latest`, which could
# silently be a different build.
lenny_export_image_tags() {
    local svc var tag
    for svc in $LENNY_IMAGE_SERVICES; do
        var="$(lenny_svc_tag_var "$svc")"
        tag="$(bash "$_images_dir/image-hash.sh" tag "$svc")"
        export "$var=$tag"
    done
}

# Guidance printed when a NODE image (reader/admin/opds) is missing from ghcr on
# a production path. Written for an operator reading it at 2am: it names the
# image and tag, says where the tag comes from, and gives the exact opt-in.
lenny_missing_image_error() {
    local svc="$1" tag="$2"
    local image="ghcr.io/${LENNY_IMAGE_ORG:-archivelabs}/lenny-$svc"
    {
        echo ""
        echo "[images] ERROR: no prebaked image  $image:$tag"
        echo "[images]   This tag is the content hash of ${svc}'s inputs. CI publishes it on merge"
        echo "[images]   to main (.github/workflows/docker-publish.yml). If you are on an unmerged"
        echo "[images]   branch, or the image is still building, it does not exist yet."
        if [ "$svc" = "admin" ]; then
            echo "[images]   NOTE: a custom NEXT_PUBLIC_API_URL is folded into this hash BY DESIGN, so a"
            echo "[images]   non-empty value can NEVER match a published image. Either leave it empty to"
            echo "[images]   use the shared image, or build locally (below)."
        fi
        echo "[images]   To build it on THIS host anyway — needs adequate RAM/swap; the reader's"
        echo "[images]   pnpm build has OOM-killed ~1GB VPSes and taken the whole API down with it:"
        echo "[images]       LENNY_ALLOW_LOCAL_BUILD=1 make start      # or: make update"
        echo ""
    } >&2
}

# Resolve every prebaked image by its content-hash tag and ensure each is
# present locally, so a following `up -d --no-build` can start the whole stack.
#
# Miss policy (a node image missing at RUNTIME takes the API down too — nginx
# refuses to start when a proxy_pass upstream is unresolvable — so a partial
# stack is a worse outcome than refusing):
#   - api            auto-builds. It is apt + uv (cheap) and its inputs (lenny/**)
#                    change every release, so a miss is the normal case.
#   - reader/admin/opds  FAIL LOUDLY and do not auto-build. The reader's pnpm build
#                    is the single most expensive thing in the stack; auto-building
#                    it on a ~1GB VPS is exactly the OOM outage this change exists to
#                    remove. Override deliberately with LENNY_ALLOW_LOCAL_BUILD=1.
# Returns non-zero WITHOUT touching any container when a node image is missing
# and the override is off, so the caller must not proceed to `up`.
lenny_pull_images() {
    local compose="${COMPOSE_CMD:-docker compose}"
    local project="${LENNY_COMPOSE_PROJECT:-lenny}"
    local allow_build="${LENNY_ALLOW_LOCAL_BUILD:-0}"
    local svc var tag build_list="" fatal=0

    lenny_export_image_tags

    for svc in $LENNY_IMAGE_SERVICES; do
        var="$(lenny_svc_tag_var "$svc")"
        tag="${!var}"
        echo "[images] $svc -> $tag"
        if $compose -p "$project" pull "$svc" 2>/dev/null; then
            continue
        fi
        if [ "$svc" = "api" ]; then
            echo "[images] no prebaked api:$tag — building locally (cheap; api inputs change every release)."
            build_list="$build_list api"
        elif [ "$allow_build" = "1" ]; then
            echo "[images] no prebaked $svc:$tag — LENNY_ALLOW_LOCAL_BUILD=1 set, building locally."
            build_list="$build_list $svc"
        else
            fatal=1
            lenny_missing_image_error "$svc" "$tag"
        fi
    done

    if [ "$fatal" = "1" ]; then
        echo "[images] Refusing to start a partial stack — no containers were touched." >&2
        return 1
    fi

    if [ -n "$build_list" ]; then
        echo "[images] building locally:$build_list"
        # shellcheck disable=SC2086
        $compose -p "$project" build $build_list
    fi
}
