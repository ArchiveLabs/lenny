FROM node:22-slim AS base

# Set environment variables for PNPM
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
ENV CI=true

# Build arguments - values provided by compose.yaml or --build-arg
ARG NODE_ENV
ARG MANIFEST_ROUTE_FORCE_ENABLE
ARG MANIFEST_ALLOWED_DOMAINS

# Convert ARGs to ENV so Next.js can access them during build
ENV NODE_ENV=$NODE_ENV
ENV MANIFEST_ROUTE_FORCE_ENABLE=$MANIFEST_ROUTE_FORCE_ENABLE
ENV MANIFEST_ALLOWED_DOMAINS=$MANIFEST_ALLOWED_DOMAINS

WORKDIR /app

RUN apt-get update && apt-get install -y git

# Pin thorium-web to a specific commit (default branch: develop).
# To upgrade the reader: replace THORIUM_REF with a newer commit SHA. Changing
# this value invalidates the build cache from this line onward — deterministic
# on both BuildKit and the classic builder — so `make update` re-clones and
# checks out the new commit. No floating HEAD = no surprise upstream drift.
# v1.7.0
ARG THORIUM_REF=8d49c2b116e93ca50d4ccfdcb3226efde7bc49c8

RUN git clone https://github.com/edrlab/thorium-web.git && \
    cd thorium-web && \
    git checkout "$THORIUM_REF"
    
# Upstream compares hostnames exactly, so the default `*.trycloudflare.com` entry never matches.
# Let a leading `*.` match any subdomain, so a tunnel URL that changes on restart still passes.
RUN node -e '\
const f="thorium-web/src/next-lib/helpers/verifyManifest.ts",fs=require("fs"),s=fs.readFileSync(f,"utf8"),\
o="return url.hostname === domainUrl.hostname;";\
if(s.split(o).length!==2)throw new Error("verifyManifest anchor not found");\
fs.writeFileSync(f,s.replace(o,"return url.hostname === domainUrl.hostname || (domainUrl.hostname.startsWith(\"*.\") \&\& url.hostname.endsWith(domainUrl.hostname.slice(1)));"))'

RUN corepack enable

WORKDIR /app/thorium-web

# SECURITY: the pinned THORIUM_REF ships Next.js 16.2.12, which is a vulnerable
# release; 16.4.1 is the patched version (upstream has not bumped its own pin).
# We override it in OUR build rather than moving THORIUM_REF (that pin is
# deliberate; see the comment above it). Two moves are needed because a later
# `pnpm install` would otherwise ignore a bare package.json edit:
#   1. edit the direct dependency in package.json, and
#   2. add a pnpm `overrides` entry (below) so any transitive copy is forced too.
# The build then re-resolves (install is --no-frozen-lockfile for this reason)
# and a hard assertion after install fails the build if next is still < 16.4.1,
# so a lockfile that quietly ignored the edit cannot ship a vulnerable image.
RUN node -e 'const fs=require("fs"),p="package.json",j=JSON.parse(fs.readFileSync(p,"utf8"));if(!(j.dependencies&&j.dependencies.next))throw new Error("next is not a direct dependency of thorium-web package.json; the security override anchor is stale");j.dependencies.next="16.4.1";fs.writeFileSync(p,JSON.stringify(j,null,2)+"\n")'

# pnpm now blocks build scripts for any command — even an explicit `pnpm
# rebuild <pkg>` below — unless the package is allow-listed via `allowBuilds`
# in pnpm-workspace.yaml (package.json's old `pnpm.onlyBuiltDependencies`
# field is no longer read). thorium-web ships no such list, and there's no
# TTY here for the interactive `pnpm approve-builds`, so write it directly.
# `overrides: next` forces the patched version across the whole tree (direct and
# any transitive). It lives here (pnpm-workspace.yaml) because this build makes
# thorium-web a workspace root; the package.json edit above keeps the direct
# dependency honest too.
RUN printf 'allowBuilds:\n  esbuild: true\n  sharp: true\n  unrs-resolver: true\noverrides:\n  next: 16.4.1\n' > pnpm-workspace.yaml

# --no-frozen-lockfile (was --frozen-lockfile): the next override above changes
# what the lockfile must satisfy, so pnpm has to re-resolve and update the
# in-container lockfile. A frozen install would instead fail (or silently keep
# the vulnerable pin). The assertion below is the real guarantee regardless.
RUN --mount=type=cache,id=pnpm_store,target=/pnpm/store pnpm install --no-frozen-lockfile --ignore-scripts

# Fail the build NOW (before the expensive TypeScript build) if the override did
# not take. This is the backstop against a green build still shipping a
# vulnerable Next.js — the exact failure mode a silent lockfile would produce.
RUN node -e 'const v=require("next/package.json").version,[a,b,c]=v.split(".").map(Number),ok=a>16||(a===16&&(b>4||(b===4&&c>=1)));if(!ok){console.error("SECURITY: next resolved to "+v+" but >=16.4.1 is required");process.exit(1)}console.log("OK: next resolved to "+v)'

RUN pnpm rebuild esbuild sharp unrs-resolver

RUN pnpm build

EXPOSE 3000

CMD ["pnpm", "start"]
