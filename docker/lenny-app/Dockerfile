FROM node:20-slim AS base
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

WORKDIR /app
RUN apt-get update && apt-get install -y git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN corepack enable


RUN git clone https://github.com/ArchiveLabs/lenny-app .

# Install all workspace deps (no cache mount — fully clean install)
RUN pnpm install --frozen-lockfile

# Build only the web app
ARG NEXT_PUBLIC_API_URL=
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

RUN pnpm turbo run build --filter=web

EXPOSE 4000

WORKDIR /app/apps/web
ENV PORT=4000
CMD ["pnpm", "start"]
