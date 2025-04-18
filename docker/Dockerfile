# Use official Python runtime as base image
FROM python:3.11

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set non-sensitive environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Expose ports (FastAPI, PostgreSQL, MinIO API, MinIO Console)
EXPOSE 8080 5432 9000 9001

# Copy application code, including migrations and alembic.ini
COPY ./lenny/ /app/lenny/
COPY alembic.ini /app/alembic.ini
RUN chmod +x /app/lenny/app.py

# Install system dependencies for PostgreSQL client, NGINX, and pg_isready
RUN apt-get update && apt-get install -y \
    libpq-dev \
    nginx \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Setup NGINX
RUN adduser --disabled-password --gecos "" nginx
COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/conf.d/lenny.conf /etc/nginx/conf.d/lenny.conf

# Copy run script
COPY ./docker/run.sh /app/run.sh
RUN chmod +x /app/run.sh

# Use run script to handle migrations and start services
CMD ["/app/run.sh"]