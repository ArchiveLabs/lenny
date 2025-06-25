FROM golang:1.21 AS builder

WORKDIR /build

# Install git and clone the Readium LCP server source code
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/readium/readium-lcp-server.git .

# Build the binaries
RUN go build -o /bin/lcpencrypt ./lcpencrypt && \
    go build -o /bin/lcpserver ./lcpserver && \
    go build -o /bin/lsdserver ./lsdserver

FROM debian:bookworm-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*

COPY --from=builder /bin/lcpencrypt /usr/local/bin/lcpencrypt
COPY --from=builder /bin/lcpserver /usr/local/bin/lcpserver
COPY --from=builder /bin/lsdserver /usr/local/bin/lsdserver

RUN chmod +x /usr/local/bin/lcpencrypt /usr/local/bin/lcpserver /usr/local/bin/lsdserver

RUN mkdir -p /srv/config /srv/db /srv/tmp

EXPOSE 8989 8990
