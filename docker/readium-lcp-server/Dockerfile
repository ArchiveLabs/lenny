FROM golang:1.21 AS builder

WORKDIR /build

COPY ../../readium/readium-lcp-server/ ./

RUN go build -o /bin/lcpencrypt ./lcpencrypt && \
    go build -o /bin/lcpserver ./lcpserver && \
    go build -o /bin/lsdserver ./lsdserver

FROM debian:bookworm-slim

WORKDIR /srv

RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*

COPY --from=builder /bin/lcpencrypt /usr/local/bin/lcpencrypt
COPY --from=builder /bin/lcpserver /usr/local/bin/lcpserver
COPY --from=builder /bin/lsdserver /usr/local/bin/lsdserver

RUN mkdir -p /srv/config /srv/cert /srv/db /srv/tmp
COPY ../../readium/config/ /srv/config/
COPY ../../readium/cert/ /srv/cert/
COPY ../../readium/db/ /srv/db/
COPY ../../readium/tmp/ /srv/tmp/

EXPOSE 8989 8990


ENTRYPOINT ["/usr/local/bin/lcpserver"]
CMD ["-config", "/srv/config/config.yaml"]
