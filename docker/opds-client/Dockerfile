FROM node:22-alpine AS build
WORKDIR /app
RUN apk add --no-cache git python3

# Pin opds-client to a specific commit (default branch: main). To upgrade the
# frontend: replace OPDS_CLIENT_REF with a newer commit SHA. Changing it
# invalidates the build cache from this line onward, so `make update` re-clones.
ARG OPDS_CLIENT_REF=7634fb42b716e66676e8d96ec042239a3812f1d7

RUN git clone https://github.com/ArchiveLabs/opds-client . && \
    git checkout "$OPDS_CLIENT_REF"

# Lock the client to this node: default feed is Lenny's own /v1/api/opds, and
# any other origin (?opds=..., alternate links) is refused. See the script.
COPY docker/opds-client/lock-to-lenny.py /tmp/lock-to-lenny.py
RUN python3 /tmp/lock-to-lenny.py

RUN --mount=type=cache,target=/root/.npm npm ci
RUN npm run build

FROM nginx:1.29-alpine
COPY --from=build /app/build /usr/share/nginx/html
COPY docker/opds-client/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
