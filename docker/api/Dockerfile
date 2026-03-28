FROM python:3.12

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set non-sensitive environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose ports (FastAPI, PostgreSQL, MinIO API, MinIO Console)
# XXX These ports should be variables to reflect compose.yml
EXPOSE 8080 5432 9000 9001 3000 15080

COPY ./VERSION /app/VERSION
COPY ./lenny/ /app/lenny/
COPY ./alembic/ /app/alembic/
COPY ./alembic.ini /app/alembic.ini
RUN chmod +x /app/lenny/app.py

RUN apt-get update && apt-get install -y \
    nginx \
    libpq-dev \
    postgresql-client
RUN adduser --disabled-password --gecos "" nginx

COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/conf.d/lenny.conf /etc/nginx/conf.d/lenny.conf

# Migration script (service startup is inline below)
COPY ./docker/utils/migrate.sh /app/docker/utils/migrate.sh
RUN chmod +x /app/docker/utils/migrate.sh

CMD ["sh", "-c", "sh /app/docker/utils/migrate.sh && python -m uvicorn lenny.app:app --host 0.0.0.0 --port 1337 --workers=${LENNY_WORKERS:-$([ \"${LENNY_PRODUCTION:-true}\" = \"false\" ] && echo 1 || echo 3)} --log-level=${LENNY_LOG_LEVEL:-info} $([ \"${LENNY_PRODUCTION:-true}\" = \"false\" ] && echo --reload) & exec nginx"]
