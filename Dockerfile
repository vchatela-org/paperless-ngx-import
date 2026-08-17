# Alpine keeps the runtime free of the Debian packages that carry permanently
# unfixed CVEs (perl-base alone accounts for 4 criticals on slim-trixie with no
# fix available upstream). Pinned to a stable release + digest so builds are
# reproducible and Dependabot can propose updates deterministically.
FROM python:3.14.7-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc AS builder

WORKDIR /app

# Install into an isolated prefix so the runtime stage can take the packages
# without inheriting pip, setuptools and their vendored dependencies.
# Every dependency ships a pure-Python wheel, so musl needs no toolchain here.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --prefix=/install -r requirements.txt


FROM python:3.14.7-alpine@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc

WORKDIR /app

# Apply OS security updates. curl comes along for the ride: CronJob wrappers
# and readiness probes reach for it, and its absence used to be swallowed as a
# silent "run anyway" instead of a failed precondition.
RUN apk --no-cache upgrade \
    && apk add --no-cache curl

COPY --from=builder /install /usr/local

# The job needs nothing but `requests` at runtime. Removing the build tooling
# eliminates a standing source of scanner findings (pip vendors its own copy of
# msgpack, and setuptools trails its upstream fixes in base images).
# The interpreter reports its own site-packages: hardcoding python3.X here would
# turn the cleanup into a silent no-op the next time the base image moves, and
# the uninstall above it is best-effort, so the removal has to actually land.
RUN SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && { python -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true; } \
    && rm -rf "$SITE"/pip "$SITE"/pip-* \
              "$SITE"/setuptools "$SITE"/setuptools-* \
              "$SITE"/pkg_resources \
              "$SITE"/wheel "$SITE"/wheel-* \
              /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
    && ! python -c "import setuptools" 2>/dev/null \
    && ! python -c "import pip" 2>/dev/null \
    && python -c "import requests; print('runtime deps OK:', requests.__version__)"

# App code
COPY import_to_paperless_docker.py import_to_paperless.py

# Non-root user (BusyBox adduser/addgroup syntax)
RUN addgroup -S paperless \
    && adduser -S -G paperless paperless \
    && mkdir -p /app/logs \
    && chown -R paperless:paperless /app
USER paperless

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WATCH_DIR=/mnt/documents

CMD ["python", "import_to_paperless.py"]
