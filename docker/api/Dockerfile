FROM python:3.12

WORKDIR /app

# ── Stable layers first ──────────────────────────────────────────────────────
# Everything below is ordered least-volatile to most-volatile, because Docker
# invalidates every layer after the first one that changes. `apt-get` used to
# sit BELOW `COPY ./lenny/`, so editing any Python file re-ran it: a code-only
# rebuild took 32s, of which ~24s was reinstalling nginx and postgresql-client
# that had not changed. Keep new instructions in the right band.
RUN apt-get update && apt-get install -y \
    nginx \
    libpq-dev \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*
# Debian's nginx package creates `www-data`, not `nginx`, so this does not clash.
RUN adduser --disabled-password --gecos "" nginx

# ── Python dependencies ──────────────────────────────────────────────────────
# uv rather than pip: it resolves and installs the pinned set in a fraction of
# the time, which is most of a cold build. Pinned by digest-bearing tag so a
# rebuild months from now installs what this one did — `:latest` here would
# make the build silently non-reproducible.
#
# `--system` installs into the image's own Python rather than a venv, matching
# what pip did, so PYTHONPATH and the CMD below are unchanged.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# Set non-sensitive environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose ports (FastAPI, PostgreSQL, Garage S3 API)
# XXX These ports should be variables to reflect compose.yml
EXPOSE 8080 5432 9000 3000 15080

# nginx.conf is baked in; conf.d is ALSO bind-mounted by compose, so a conf.d
# that references a limit_req_zone this image lacks makes nginx refuse to start.
COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/conf.d/lenny.conf /etc/nginx/conf.d/lenny.conf

# Migration script (service startup is inline below)
COPY ./docker/utils/migrate.sh /app/docker/utils/migrate.sh
RUN chmod +x /app/docker/utils/migrate.sh

# ── Volatile layers last ─────────────────────────────────────────────────────
COPY ./VERSION /app/VERSION
COPY ./alembic.ini /app/alembic.ini
COPY ./alembic/ /app/alembic/
COPY ./lenny/ /app/lenny/
RUN chmod +x /app/lenny/app.py

# --forwarded-allow-ips must be a BOUNDED range, never '*'. nginx sets
# X-Forwarded-For with `$proxy_add_x_forwarded_for`, which appends the address
# it observed to whatever the client sent; uvicorn trusting everything takes the
# LEFTMOST entry, i.e. the client's own value, letting a caller pick the IP
# Lenny binds sessions and OTPs to (ArchiveLabs/lenny#210). Bounded, uvicorn
# walks right-to-left to the first untrusted hop — the address nginx saw.
# Never set this to an empty string: that trusts no proxy at all and collapses
# every patron onto the nginx container's address.
#
# --forwarded-allow-ips: nginx runs in this same container and reverse-proxies to
# :1337 over the container's own address, which is NOT 127.0.0.1 — so uvicorn's
# default trusted-proxy list rejects the X-Forwarded-For nginx sets, and
# `request.client.host` collapses to the proxy's address for EVERY patron. That
# address is what Lenny sends Open Library as the OTP's `ip`, so all patrons on an
# instance shared one identity and one rate-limit bucket. Port 1337 is never
# published (only nginx's :80 is), so nginx is the only possible peer here.
# Override with LENNY_FORWARDED_ALLOW_IPS if you front Lenny with your own proxy.
CMD ["sh", "-c", "sh /app/docker/utils/migrate.sh && python -m uvicorn lenny.app:app --host 0.0.0.0 --port 1337 --proxy-headers --forwarded-allow-ips=\"${LENNY_FORWARDED_ALLOW_IPS:-172.16.0.0/12}\" --workers=${LENNY_WORKERS:-$([ \"${LENNY_PRODUCTION:-true}\" = \"false\" ] && echo 1 || echo 3)} --log-level=${LENNY_LOG_LEVEL:-info} $([ \"${LENNY_PRODUCTION:-true}\" = \"false\" ] && echo --reload) & exec nginx"]
