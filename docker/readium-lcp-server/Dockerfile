# Use official Golang image for building
FROM golang:1.21 AS builder

WORKDIR /build

# Copy the Readium LCP server source code (assume it's in /readium/readium-lcp-server)
COPY ../../readium/readium-lcp-server/ ./

# Build the binaries
RUN go build -o /bin/lcpencrypt ./lcpencrypt && \
    go build -o /bin/lcpserver ./lcpserver && \
    go build -o /bin/lsdserver ./lsdserver

# Final runtime image
FROM debian:bookworm-slim

WORKDIR /srv

# Install runtime dependencies
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*

# Copy built binaries
COPY --from=builder /bin/lcpencrypt /usr/local/bin/lcpencrypt
COPY --from=builder /bin/lcpserver /usr/local/bin/lcpserver
COPY --from=builder /bin/lsdserver /usr/local/bin/lsdserver

# Copy config, cert, and db folders (mounted at runtime, but provide structure)
RUN mkdir -p /srv/config /srv/cert /srv/db /srv/tmp
COPY ../../readium/config/ /srv/config/
COPY ../../readium/cert/ /srv/cert/
COPY ../../readium/db/ /srv/db/
COPY ../../readium/tmp/ /srv/tmp/

# Expose default ports for lcpserver and lsdserver
EXPOSE 8989 8990

# Entrypoint: run lcpserver by default, can be overridden
ENTRYPOINT ["/usr/local/bin/lcpserver"]
CMD ["-config", "/srv/config/config.yaml"]
