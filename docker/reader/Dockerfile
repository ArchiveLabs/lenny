FROM node:20-slim AS base

# Set environment variables for PNPM (optional, but good practice)
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

ARG NODE_ENV
ARG NEXT_PUBLIC_ENABLE_MANIFEST_ROUTE
ARG NEXT_PUBLIC_MANIFEST_FORCE_ENABLE
ARG NEXT_PUBLIC_MANIFEST_ALLOWED_DOMAINS
ENV NODE_ENV=$NODE_ENV

WORKDIR /app

RUN apt-get update && apt-get install -y git

RUN git clone https://github.com/edrlab/thorium-web.git
RUN corepack enable

WORKDIR /app/thorium-web

RUN --mount=type=cache,id=pnpm_store,target=/pnpm/store pnpm install --frozen-lockfile

RUN pnpm build

EXPOSE 3000

CMD ["pnpm", "start"]
