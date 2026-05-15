FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/data

ENV DB_PATH=/app/data/proxy.db \
    ADMIN_TOKEN=changeme \
    CLIENT_API_KEY=changeme \
    UPSTREAM_API_KEY="" \
    REQUEST_TIMEOUT=60 \
    HEALTH_CHECK_TIMEOUT=10 \
    REVIVAL_CHECK_INTERVAL=30 \
    URL_SYNC_INTERVAL=3600 \
    URL_SYNC_FILE="" \
    MAX_CALLS_BEFORE_CHECK=3 \
    DEFAULT_MODEL=gpt-4o-mini

EXPOSE 7788

CMD ["gunicorn", "app.main:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--bind", "0.0.0.0:7788", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
