#FROM python:3.13-alpine3.22
FROM python:3.13.7-slim

WORKDIR /app

# Upgrade base OS packages (for security)
RUN apk upgrade --no-cache

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY import_to_paperless_docker.py import_to_paperless.py

# Non-root user
RUN addgroup --system paperless && adduser --system --ingroup paperless paperless \
    && mkdir -p /app/logs \
    && chown -R paperless:paperless /app
USER paperless

ENV PYTHONUNBUFFERED=1 \
    WATCH_DIR=/mnt/documents

CMD ["python", "import_to_paperless.py"]
