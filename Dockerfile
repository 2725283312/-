FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV DB_PATH=/data/proxy.db
ENV ADMIN_TOKEN=QQliutao011007
ENV REQUEST_TIMEOUT=60
ENV HEALTH_CHECK_TIMEOUT=10
ENV REVIVAL_CHECK_INTERVAL=30
ENV URL_SYNC_INTERVAL=3600
ENV URL_SYNC_FILE=
ENV URL_SYNC_GROUP_ID=0
ENV MAX_CALLS_BEFORE_CHECK=3
ENV DEFAULT_MODEL=gpt-4o-mini

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
