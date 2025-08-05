FROM python:3.12-alpine

# Set working directory
WORKDIR /app

# Install system dependencies if needed (Alpine uses apk instead of apt)
RUN apk update && apk upgrade && apk add --no-cache \
    && rm -rf /var/cache/apk/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the import script
COPY import_to_paperless_docker.py import_to_paperless.py

# Create directory for logs
RUN mkdir -p /app/logs

# Set environment variables with defaults
ENV PYTHONUNBUFFERED=1
ENV WATCH_DIR=/mnt/documents

# Create non-root user for security
RUN addgroup -S paperless && adduser -S paperless -G paperless
RUN chown -R paperless:paperless /app
USER paperless

# Run the import script
CMD ["python", "import_to_paperless.py"]
