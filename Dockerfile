# Pinned to a stable release + digest so builds are reproducible and Dependabot
# can propose (and scanners can attribute) base image updates deterministically.
FROM python:3.14.7-slim-trixie@sha256:83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910 AS builder

WORKDIR /app

# Install into an isolated prefix so the runtime stage can take the packages
# without inheriting pip, setuptools and their vendored dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt


FROM python:3.14.7-slim-trixie@sha256:83c1cebb322d099ac9e3a3a532ba74b0146d702838b25e4c75c02fa81ffeb910

WORKDIR /app

# Apply OS security updates, then drop the package lists so they do not ship in
# the final layer (smaller image, fewer stale CVE references for scanners).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# The job needs nothing but `requests` at runtime. Removing the build tooling
# eliminates a standing source of scanner findings (pip vendors its own copies
# of msgpack et al., and setuptools trails its upstream fixes in base images).
RUN python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
    && rm -rf /usr/local/lib/python3.14/site-packages/pip \
              /usr/local/lib/python3.14/site-packages/pip-* \
              /usr/local/lib/python3.14/site-packages/setuptools \
              /usr/local/lib/python3.14/site-packages/setuptools-* \
              /usr/local/lib/python3.14/site-packages/pkg_resources \
              /usr/local/lib/python3.14/site-packages/wheel \
              /usr/local/lib/python3.14/site-packages/wheel-* \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
    && python -c "import requests; print('runtime deps OK:', requests.__version__)"

# App code
COPY import_to_paperless_docker.py import_to_paperless.py

# Non-root user
RUN addgroup --system paperless && adduser --system --ingroup paperless paperless \
    && mkdir -p /app/logs \
    && chown -R paperless:paperless /app
USER paperless

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATCH_DIR=/mnt/documents

CMD ["python", "import_to_paperless.py"]
