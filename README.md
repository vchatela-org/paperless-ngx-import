# Paperless-NGX Import

A containerized job that bulk-imports documents into **Paperless-NGX**, with **deduplication, folder-derived tagging, logging and task-queue monitoring**.

Designed to run as a Kubernetes CronJob (or a one-shot `docker run` / `docker compose`) against a read-only document share.

---

## Features

- 🔄 Deduplication by checksum, with filename fallback
- 🏷️ Automatic tag creation from the folder structure
- 📂 Read-only document mount (NFS or local)
- 🐳 Non-root, read-only-rootfs container
- 📊 Detailed logging, stuck-task detection and queue draining
- 🔐 Secrets supplied purely via environment (Vault, sealed secrets, `.env` — your choice)

---

## Configuration

All configuration is read from environment variables.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `PAPERLESS_API_URL` | ✅ | — | Base API URL, e.g. `https://paperless.example.com/api`. Must be `https`. |
| `PAPERLESS_API_TOKEN` | ✅ | — | Paperless-NGX API token. |
| `WATCH_DIR` | | `/mnt/documents` | Directory tree to scan. |
| `IGNORED_PATHS` | | `/mnt/` | Path prefixes stripped before deriving tags. |
| `IGNORED_FOLDERS` | | `#recycle,@eaDir` | Folder names to skip entirely. |
| `IGNORED_EXTENSIONS` | | see below | File extensions to skip. |
| `LOG_RETENTION_DAYS` | | `30` | Age at which old log files are deleted. |
| `PAPERLESS_ALLOW_INSECURE_HTTP` | | unset | Set to `true` to permit a plaintext `http://` API URL on a trusted network. |

Default ignored extensions: `.url .pkpass .xlsx .xls .html .htm .ini .lnk .exe .msi .bat .cmd .doc .docx .db .mp4 .zip .log`

> The API token is sent on every request, so the script refuses a non-`https`
> `PAPERLESS_API_URL` unless you explicitly opt out. The token is also redacted
> from anything written to stdout or the log files.

---

## Running

### Docker

```bash
docker run --rm \
  -e PAPERLESS_API_URL="https://paperless.example.com/api" \
  -e PAPERLESS_API_TOKEN="$PAPERLESS_API_TOKEN" \
  -v /path/to/documents:/mnt/documents:ro \
  -v "$PWD/logs:/app/logs" \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  registry.example.com/paperless-ngx-import/paperless-import:latest
```

### Docker Compose

Put your secrets in a gitignored `.env`, then:

```bash
docker compose up
```

### Kubernetes

Run the image as a `CronJob` with:

- the document share mounted read-only at `/mnt/documents`
- `PAPERLESS_API_TOKEN` injected from your secret store
- `securityContext`: `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`

### Building locally

```bash
REGISTRY_URL=registry.example.com ./build-and-push.sh [tag]
```

---

## How it works

1. Walks `WATCH_DIR`, newest files first
2. Skips ignored folders and extensions
3. Skips documents already in Paperless (MD5 checksum, then exact filename)
4. Derives tags from the parent folder names, creating any that are missing
5. Uploads the remaining documents and records their task IDs
6. Acknowledges completed tasks, then waits for the queue to drain

Task queue states — **active**: `PENDING`, `RECEIVED`, `STARTED`, `RETRY`; **terminal**: `SUCCESS`, `FAILURE`, `REVOKED`.

Tasks stuck for more than 5 minutes trigger a one-time queue clear. Waiting is capped at one hour overall.

---

## Dependencies

`requirements.txt` is fully pinned with hashes and installed with `pip --require-hashes`. To change a dependency, edit `requirements.in` and regenerate:

```bash
uv pip compile requirements.in --generate-hashes --python-version 3.14 --output-file requirements.txt
```

---

## Logging & exit codes

Logs go to `stdout` (for cluster log collection) and to `/app/logs/paperless_import_YYYYMMDD.log`, pruned per `LOG_RETENTION_DAYS`.

| Exit code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Completed with recoverable errors (e.g. some uploads failed) |
| `2` | Critical error (bad configuration, missing watch directory) |

---

## Troubleshooting

- **`Configuration error: PAPERLESS_API_URL must use https`** → use an `https` URL, or set `PAPERLESS_ALLOW_INSECURE_HTTP=true` on a trusted network
- **API failures** → verify `PAPERLESS_API_URL` (it must include `/api`) and the token
- **No files found** → check the volume mount and `WATCH_DIR`
- **Permission issues** → the container runs as a non-root system user; the document mount must be readable by it

---

## Security

Vulnerability reports: see [SECURITY.md](SECURITY.md). This repository has CodeQL
code scanning, secret scanning with push protection, and Dependabot enabled.

## License

[MIT](LICENSE)
