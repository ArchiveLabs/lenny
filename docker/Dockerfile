FROM python:3.11

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Set only non-sensitive environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

EXPOSE 8080 5432 9000 9001

COPY ./lenny/ /app/lenny/
RUN chmod +x /app/lenny/app.py

# Setup NGINX
RUN apt-get update && apt-get install -y nginx
RUN adduser --disabled-password --gecos "" nginx
COPY ./docker/nginx/nginx.conf /etc/nginx/nginx.conf
COPY ./docker/nginx/conf.d/lenny.conf /etc/nginx/conf.d/lenny.conf

# Run FastAPI app and Nginx
CMD ["sh", "-c", "python -m uvicorn lenny.app:app --host 0.0.0.0 --port 1337 --workers=${LENNY_WORKERS:-1} --log-level=${LENNY_LOG_LEVEL:-info} & exec nginx"]
