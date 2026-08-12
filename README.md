# Paperless-NGX Import

A containerized job that bulk-imports documents into **Paperless-NGX**, with **deduplication, folder-derived tagging, logging and task-queue monitoring**.

Designed to run as a Kubernetes CronJob (or a one-shot `docker run` / `docker compose`) against a read-only document share.

---

## Features

- 🔄 Deduplication from a **local state file** — works even while the API is down
- 🏷️ Automatic tag creation from the folder structure, resolved once per run
- 🚦 **Backpressure**: never submits faster than Paperless retires OCR tasks
- 🩺 Preflight health check, circuit breaker, bounded retries with `Retry-After`
- 📏 Per-run upload cap and pacing, so a catch-up spreads over days
- 📂 Read-only document mount (NFS or local)
- 🐳 Non-root, read-only-rootfs container
- 🔐 Secrets supplied purely via environment (Vault, sealed secrets, `.env` — your choice)

---

## Designed for a small, sometimes-absent backend

This importer assumes Paperless may be slow, saturated, or scaled to zero, and
that OCR is the scarcest resource in the cluster. Five mechanisms keep it in
its lane:

| Mechanism | What it prevents |
| --- | --- |
| **Preflight** (`GET /api/status/`) | Walking the whole tree and failing every upload when Paperless is down. One request, then `exit 0`. |
| **Circuit breaker** | Grinding for 70 minutes against a dead backend. Aborts after N consecutive connection errors or 5xx. |
| **Backpressure** | Dumping thousands of OCR tasks onto a 4-core node. Pauses whenever the task queue exceeds `QUEUE_DEPTH_LIMIT`. Needs a token that may read `/api/tasks/`. |
| **Per-run cap + pacing** | A first-time or post-outage catch-up landing in one burst. |
| **Local state file** | Re-submitting everything after an outage, because API-based dedup returned "0 already exist". |

A backlog of several thousand documents is therefore imported over several
days, a few hundred at a time, and only ever as fast as Paperless can keep up.

---

## Configuration

All configuration is read from environment variables.

### Core

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

Default ignored extensions: `.url .pkpass .xlsx .xls .html .htm .ini .lnk .exe .msi .bat .cmd .doc .docx .db .mp4 .zip .log .pag .dir .apk .lic`

### Rate limiting and resilience

Defaults are tuned for a small homelab (a handful of nodes, a few cores each).

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_UPLOADS_PER_RUN` | `200` | Hard cap on submissions per run. `0` disables the cap. |
| `UPLOAD_DELAY_SECONDS` | `5` | Pause between submissions. |
| `QUEUE_DEPTH_LIMIT` | `25` | Pause uploads while Paperless has more than this many `PENDING`+`STARTED` tasks. Requires read access to `/api/tasks/`. |
| `QUEUE_POLL_INTERVAL` | `15` | Seconds between queue re-polls while paused. |
| `QUEUE_DRAIN_TIMEOUT` | `1800` | Give up waiting for the queue after this long and end the run cleanly. |
| `MAX_CONSECUTIVE_FAILURES` | `10` | Circuit breaker: abort after this many consecutive failed API calls. |
| `MAX_RETRIES` | `4` | Retries per request on `429`/`5xx` and connection errors. |
| `RETRY_BACKOFF_SECONDS` | `5` | Initial retry backoff; doubles each attempt. |
| `RETRY_MAX_DELAY_SECONDS` | `120` | Ceiling for backoff and for a server-supplied `Retry-After`. |
| `PREFLIGHT_ENABLED` | `true` | Check the API is up and the token works before doing any work. |
| `STATE_FILE` | `/app/logs/import_state.json` | Local record of what has been imported. |
| `WAIT_FOR_QUEUE_ON_FINISH` | `false` | Block at the end of the run until the queue drains (see below). |
| `QUEUE_WAIT_TIMEOUT` | `3600` | Cap on that end-of-run wait. |

> **Sizing the knobs.** `QUEUE_DEPTH_LIMIT` is the one that protects your
> cluster: set it to roughly the number of OCR tasks you are happy to have
> outstanding at once. `MAX_UPLOADS_PER_RUN` only bounds how long a single run
> lasts — with a daily CronJob, `200` clears a 6 000-document backlog in about a
> month, so raise it if you would rather absorb the backlog faster.

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
- **a persistent volume at `/app/logs`** — this holds `STATE_FILE`, and losing it
  means the next run re-checks every document against the API
- `PAPERLESS_API_TOKEN` injected from your secret store
- the rate-limiting knobs from a `ConfigMap`, e.g.:

```yaml
envFrom:
  - configMapRef:
      name: paperless-import-tuning
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: paperless-import-tuning
data:
  MAX_UPLOADS_PER_RUN: "200"
  UPLOAD_DELAY_SECONDS: "5"
  QUEUE_DEPTH_LIMIT: "25"
  QUEUE_POLL_INTERVAL: "15"
  MAX_CONSECUTIVE_FAILURES: "10"
  STATE_FILE: "/app/logs/import_state.json"
```

- `securityContext`: `runAsNonRoot: true`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`

Set `backoffLimit: 0` and let the importer's own exit codes do the talking: a
down backend is already reported as success, so a retry would only repeat work
the next scheduled run will do anyway.

The image ships `curl`, so wrapper scripts and init containers can probe
Paperless without falling back to "run anyway".

### Building locally

```bash
REGISTRY_URL=registry.example.com ./build-and-push.sh [tag]
```

---

## How it works

1. **Preflight** — `GET /api/status/`. If Paperless is unreachable, or reports an
   unhealthy database/Redis/Celery, the run logs it and exits `0` without
   touching the filesystem. A bad token exits `2`. `/api/status/` is
   superuser-only, so a `403` there is not a verdict on the token: the run
   falls back to `GET /api/ui_settings/`, which any authenticated user can
   read, and only calls the token bad if *that* is refused too.
2. **Walk** `WATCH_DIR`, newest files first.
3. **Filter**, cheapest check first: ignored folders → ignored extensions →
   local state. Nothing that gets this far has cost an API call yet.
4. **Deduplicate** the survivors against Paperless (MD5 checksum, then exact
   filename), and *record the answer in the state file* so it is never asked
   twice. Selection stops as soon as `MAX_UPLOADS_PER_RUN` candidates exist, so
   a 6 000-file tree does not mean 6 000 round-trips.
5. **Resolve tags once** — the union of folder-derived tag names across the
   selected documents is created in a single pass, before any upload.
6. **Upload**, one document at a time: wait for queue capacity, submit, record
   the result in the state file, pause for `UPLOAD_DELAY_SECONDS`.

Every API call goes through one helper that retries `429`/`5xx` and connection
errors with exponential backoff (honouring `Retry-After`) and feeds the circuit
breaker. Any success resets the breaker, so one bad document cannot trip it,
but an absent backend trips it within seconds.

### Tag ordering

Tags are resolved and created **once per run, up front** — never per file
mid-upload. Previously each file created its tags before its own upload was
attempted, so a failed upload left orphan tags behind and every subsequent file
re-attempted the same creations.

### Local state

`STATE_FILE` is a JSON map on the logs volume recording each file's path, size,
mtime, MD5 and outcome (`submitted`, `exists` or `rejected`). It is written
atomically, and flushed immediately after every submission so an interrupted or
capped run resumes exactly where it stopped.

- The `size`+`mtime` pair is the fast path; the checksum catches files that were
  renamed or moved.
- Entries for files that vanish from the tree are pruned — unless the walk found
  nothing at all, so an unmounted share is not mistaken for a mass deletion.
- A missing, unreadable or unwritable state file only degrades performance: the
  run logs a warning and falls back to asking the API.

Submission is **at-least-once**. If the job is killed in the narrow window
between Paperless accepting a document and the state file being flushed, that
one document is offered again on the next run; Paperless's own checksum
deduplication rejects it during consumption.

### End-of-run queue draining

`WAIT_FOR_QUEUE_ON_FINISH` is `false` by default. Backpressure already keeps the
queue short, and holding a CronJob pod open for an hour watching someone else's
queue is the behaviour this job exists to avoid. Set it to `true` to restore the
old draining pass, including stuck-task detection: tasks stuck for more than
5 minutes trigger a one-time queue clear, capped at `QUEUE_WAIT_TIMEOUT`.

Task queue states — **active**: `PENDING`, `RECEIVED`, `STARTED`, `RETRY`; **terminal**: `SUCCESS`, `FAILURE`, `REVOKED`.

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
| `0` | Success — including "Paperless is down, nothing to do" and "per-run cap reached" |
| `1` | Completed with recoverable errors (some uploads failed, or the circuit breaker tripped) |
| `2` | Critical error (bad configuration, missing watch directory, rejected API token) |

`0` on an unreachable backend is deliberate: a scaled-to-zero Paperless is a
normal state for a homelab, not a failed job, and it should not fill your alerts
or burn a CronJob `backoffLimit`.

---

## Troubleshooting

- **`Configuration error: PAPERLESS_API_URL must use https`** → use an `https` URL, or set `PAPERLESS_ALLOW_INSECURE_HTTP=true` on a trusted network
- **API failures** → verify `PAPERLESS_API_URL` (it must include `/api`) and the token
- **No files found** → check the volume mount and `WATCH_DIR`
- **Permission issues** → the container runs as a non-root system user; the document mount must be readable by it
- **`Paperless is not reachable or not healthy; nothing to do`** → expected while the stack is scaled down; the run exits `0` and the next one resumes
- **`Could not read the existing tags`** → the tag listing came back short or failed mid-pagination. The run aborts on purpose (exit `2`): with a partial tag cache every document would be uploaded with tags missing
- **`Object violates owner / name unique constraint`** → the tag already exists but was not in the listing. The importer now looks it up and reuses it; if it still logs `tag(s) unresolved`, check that the API token's user can *view* those tags
- **`Circuit breaker tripped`** → the backend failed `MAX_CONSECUTIVE_FAILURES` calls in a row. Check Paperless, not the importer
- **`Task queue depth N exceeds limit`** → working as intended; Paperless is busy. Raise `QUEUE_DEPTH_LIMIT` only if the cluster can take it
- **`Backpressure is OFF for this run`** → the token got `401`/`403`/`404` from `/api/tasks/`, so `QUEUE_DEPTH_LIMIT` cannot throttle anything. The message quotes the server's reply. Either grant that user permission to view tasks, or accept the degradation and size `UPLOAD_DELAY_SECONDS` / `MAX_UPLOADS_PER_RUN` to what the cluster can absorb unattended
- **`Could not read task queue depth`** (without the line above) → a transient failure, logged once per run; backpressure resumes on the next poll
- **Backlog barely shrinking** → raise `MAX_UPLOADS_PER_RUN`, lower `UPLOAD_DELAY_SECONDS`, or run the CronJob more often
- **Everything re-uploaded after an outage** → the state file is not persisted. `STATE_FILE` must live on a volume that survives the pod (the same PVC as the logs)
- **`Could not write state file`** → the logs volume is read-only or not writable by the container user; dedup falls back to the API until fixed

---

## Security

Vulnerability reports: see [SECURITY.md](SECURITY.md). This repository has CodeQL
code scanning, secret scanning with push protection, and Dependabot enabled.

## License

[MIT](LICENSE)
