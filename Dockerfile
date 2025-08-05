FROM python:3.12-slim-bullseye

# Set working directory
WORKDIR /app

# Install system dependencies if needed
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

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
RUN groupadd -r paperless && useradd -r -g paperless paperless
RUN chown -R paperless:paperless /app
USER paperless

# Run the import script
CMD ["python", "import_to_paperless.py"]
