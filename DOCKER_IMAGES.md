# Docker Image Build Process

This document explains how Lenny uses pre-built multi-architecture Docker images to speed up deployment.

## Overview

Lenny uses a hybrid approach for Docker images to balance speed and flexibility:

- **Pre-built images**: API, Database (PostgreSQL), S3 Storage (MinIO), and Readium are pre-built
- **Local build**: The Reader service is built locally because it requires dynamic configuration

## Pre-built Images

The following services use pre-built multi-architecture (linux/amd64, linux/arm64) images:

### 1. lenny-api
- **Image**: `ghcr.io/archivelabs/lenny/lenny-api:latest`
- **Build trigger**: Automated via GitHub Actions on pushes to `main` branch
- **Why pre-built**: Static configuration, no build-time environment variables needed
- **Fallback**: If the image is unavailable, Docker Compose will build it locally

### 2. Database (PostgreSQL)
- **Image**: `postgres:16` (official Docker Hub image)
- **Why pre-built**: Official PostgreSQL image, no customization needed

### 3. S3 Storage (MinIO)
- **Image**: `minio/minio:latest` (official Docker Hub image)
- **Why pre-built**: Official MinIO image, no customization needed

### 4. Readium
- **Image**: `ghcr.io/readium/readium:0.6.3` (official Readium project image)
- **Why pre-built**: Official Readium image, no customization needed

## Local Build Images

### Reader (lenny-reader)
- **Build**: Always built locally via `docker/reader/Dockerfile`
- **Why local build**: Requires `NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS` as a build argument
  - This value changes when using custom domains or cloudflared tunnels
  - Must be baked into the Next.js application at build time
- **Rebuild triggers**:
  - Running `make tunnel` (adds cloudflared domain to allowed domains)
  - Changing `NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS` in `.env`

## GitHub Actions Workflow

The `.github/workflows/build-images.yml` workflow automatically builds and pushes the `lenny-api` image when:

1. Code is pushed to the `main` branch
2. Changes are made to:
   - `docker/**` (Dockerfile changes)
   - `compose.yaml` (Docker Compose configuration)
   - `lenny/**` (application code)
   - `requirements.txt` (Python dependencies)
3. The workflow is manually triggered via GitHub Actions UI

### Workflow Details

- **Platforms**: Builds for both `linux/amd64` and `linux/arm64`
- **Registry**: Images are pushed to GitHub Container Registry (ghcr.io)
- **Tags**:
  - `latest` - always points to the most recent build from the main branch
  - `main` - also points to the main branch (same as latest)
  - `main-sha-<git-sha>` - specific commit on main branch (e.g., `main-sha-abc1234`)
  - `<branch>-sha-<git-sha>` - specific commits on other branches (if workflow runs on them)
- **Caching**: Uses GitHub Actions cache for faster builds

## Benefits

1. **Faster initial setup**: Users can pull pre-built images instead of building from scratch
2. **Consistent images**: All users get the same tested images
3. **Multi-architecture support**: Works on both x86_64 and ARM64 (Apple Silicon, ARM servers)
4. **Reduced build time**: Only the reader service needs to be built locally

## For Users

### Updating to Latest Images

To pull the latest pre-built images:

```bash
make pull
```

This will update the API, database, S3, and Readium images to their latest versions.

## For Developers

### Testing the Build Workflow

You can test the GitHub Actions workflow locally using `act`:

```bash
# Install act (https://github.com/nektos/act)
# Run the workflow locally
act push -W .github/workflows/build-images.yml
```

### Manual Build and Push

If you need to manually build and push images (requires appropriate permissions):

```bash
# Login to GitHub Container Registry
echo $GITHUB_TOKEN | docker login ghcr.io -u USERNAME --password-stdin

# Build and push multi-arch image
docker buildx create --use
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --push \
  -t ghcr.io/archivelabs/lenny/lenny-api:latest \
  -f docker/api/Dockerfile \
  .
```

### Local Development

During development, you can force local builds by:

```bash
# Remove the pre-built image
docker rmi ghcr.io/archivelabs/lenny/lenny-api:latest

# Build locally
docker compose build api

# Or use the rebuild target
make rebuild
```

## Permissions

The GitHub Actions workflow requires the following permissions:
- `contents: read` - to checkout the repository
- `packages: write` - to push images to GitHub Container Registry

The `GITHUB_TOKEN` secret is automatically provided by GitHub Actions and has the necessary permissions.

### Repository Settings

To ensure the workflow can push images to GitHub Container Registry, verify that:

1. **Workflow permissions** are set correctly in the repository settings:
   - Go to Settings > Actions > General > Workflow permissions
   - Ensure "Read and write permissions" is selected
   - Or use the specific permissions defined in the workflow file

2. **Package visibility**: After the first successful workflow run, you may want to make the package public:
   - Go to the package page: `https://github.com/orgs/<YOUR_ORG>/packages/container/<repo>%2Flenny-api`
   - For this repository, that would be: `https://github.com/orgs/ArchiveLabs/packages/container/lenny%2Flenny-api`
   - Click "Package settings"
   - Change visibility to "Public" (optional, but recommended for easier access)

3. **No additional secrets needed**: The workflow uses `GITHUB_TOKEN`, which is automatically available in GitHub Actions.
