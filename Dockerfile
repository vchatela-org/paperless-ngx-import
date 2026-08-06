# Pinned to a stable release + digest so builds are reproducible and Dependabot
# can propose (and scanners can attribute) base image updates deterministically.
FROM python:3.14.7-slim-trixie@sha256:83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910

WORKDIR /app

# Apply OS security updates, then drop the package lists so they do not ship in
# the final layer (smaller image, fewer stale CVE references for scanners).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Python deps, installed with hash verification (see requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && rm -rf /root/.cache

# App code
COPY import_to_paperless_docker.py import_to_paperless.py

# Non-root user
RUN addgroup --system paperless && adduser --system --ingroup paperless paperless \
    && mkdir -p /app/logs \
    && chown -R paperless:paperless /app
USER paperless

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WATCH_DIR=/mnt/documents

CMD ["python", "import_to_paperless.py"]
