FROM golang:1.23-alpine AS builder

WORKDIR /app

RUN apk add --no-cache git
RUN git clone https://github.com/edrlab/lcp-server.git .
RUN go get gorm.io/driver/postgres
RUN go mod tidy
RUN go mod download && go mod verify


RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -a -installsuffix cgo -ldflags \
    '-extldflags "-static"' -tags PGSQL -o /app/lcpserver ./cmd/lcpserver

RUN ls -la /app

FROM alpine:latest
WORKDIR /app

COPY --from=builder /app/lcpserver ./lcpserver

RUN ls -l /app
RUN chmod +x ./lcpserver
RUN apk add --no-cache openssl

RUN cat <<'EOF' > ./start.sh
#!/bin/sh
set -e

# Print environment variables
echo "--- Environment variables ---"
env

echo "--- Checking/generating certificates ---"
mkdir -p /app/cert
if [ ! -f /app/cert/cert-edrlab-test.pem ] || [ ! -f /app/cert/privkey-edrlab-test.pem ]; then
  echo "Generating self-signed certificate..."
  openssl req -x509 -newkey rsa:2048 -keyout /app/cert/privkey-edrlab-test.pem -out /app/cert/cert-edrlab-test.pem -days 365 -nodes -subj "/CN=lcpserver"
fi

echo "--- Starting LCP server ---"
ls -la /app/config/
cat /app/config/lcpserver.yaml
echo "File content displayed above. Now trying to run lcpserver with explicit config..."
./lcpserver -config /app/config/lcpserver.yaml


EOF
RUN chmod +x ./start.sh

ENV LCPSERVER_CONFIG=/app/config/lcpserver.yaml

ENTRYPOINT ["./start.sh"]
