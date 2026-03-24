FROM node:20-slim AS base

# Set environment variables for PNPM
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

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

RUN git clone https://github.com/edrlab/thorium-web.git && \
    cd thorium-web
    
RUN corepack enable

WORKDIR /app/thorium-web

RUN --mount=type=cache,id=pnpm_store,target=/pnpm/store pnpm install --frozen-lockfile

RUN pnpm build

EXPOSE 3000

CMD ["pnpm", "start"]
