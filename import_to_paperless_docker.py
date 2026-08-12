#!/usr/bin/env python3

"""Bulk-import a document tree into Paperless-ngx, gently.

This job runs unattended (typically as a CronJob) against a small self-hosted
Paperless instance that may be slow, saturated, or scaled to zero. Every stage
is therefore bounded:

  * a preflight health check, so a missing backend costs one request, not one
    request per file;
  * a circuit breaker, so a run can never grind for an hour against a corpse;
  * queue backpressure, so we never submit OCR work faster than the cluster
    can retire it;
  * a per-run upload cap and inter-upload delay, so a first-time or
    post-outage catch-up spreads over days instead of landing in one burst;
  * a local state file, so deduplication and resume do not depend on the API
    being reachable.
"""

import os
import sys
import time
import datetime
import json
import tempfile
import requests
import hashlib
import glob
import logging
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

# Every outbound call gets a timeout: without one, a hung Paperless instance
# would block this job forever (it runs as a CronJob with no external watchdog).
REQUEST_TIMEOUT = 30
# Document uploads stream file bodies, so they get a longer allowance.
UPLOAD_TIMEOUT = 300

# Task states that represent work Paperless has accepted but not yet retired.
# Backpressure counts these; anything else is terminal.
QUEUE_ACTIVE_STATUSES = ("PENDING", "STARTED")
# Statuses that mean "come back later" rather than "this request is wrong".
RETRYABLE_STATUSES = (429, 502, 503, 504)

# Logs and state live on the same mounted volume.
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

STATE_VERSION = 1
# Unforced state writes are batched this many changes at a time.
SAVE_BATCH_SIZE = 25

# ----------------------------
# Container Configuration via Environment Variables
# ----------------------------
def _env_int(name, default, minimum=0):
    """Read an integer knob, rejecting values that would disable a safety net."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer (got '{raw}')")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


def _env_float(name, default, minimum=0.0):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number (got '{raw}')")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_container_config():
    """Get configuration from environment variables for containerized deployment"""
    config = {
        "WATCH_DIR": os.getenv("WATCH_DIR", "/mnt/documents"),
        "PAPERLESS_API_URL": os.getenv("PAPERLESS_API_URL"),
        "PAPERLESS_API_TOKEN": os.getenv("PAPERLESS_API_TOKEN"),
        "IGNORED_PATHS": os.getenv("IGNORED_PATHS", "/mnt/").split(","),
        "IGNORED_FOLDERS": os.getenv("IGNORED_FOLDERS", "#recycle,@eaDir").split(","),
        # .pag/.dir are Synology index sidecars: Paperless rejects them as
        # empty/unsupported, so filtering them here saves the round-trip.
        "IGNORED_EXTENSIONS": os.getenv("IGNORED_EXTENSIONS", ".url,.pkpass,.xlsx,.xls,.html,.htm,.ini,.lnk,.exe,.msi,.bat,.cmd,.doc,.docx,.db,.mp4,.zip,.log,.pag,.dir,.apk,.lic").split(","),
        "LOG_RETENTION_DAYS": _env_int("LOG_RETENTION_DAYS", 30, minimum=1),

        # Preflight
        "PREFLIGHT_ENABLED": _env_bool("PREFLIGHT_ENABLED", True),

        # Circuit breaker
        "MAX_CONSECUTIVE_FAILURES": _env_int("MAX_CONSECUTIVE_FAILURES", 10, minimum=1),

        # Retries
        "MAX_RETRIES": _env_int("MAX_RETRIES", 4, minimum=0),
        "RETRY_BACKOFF_SECONDS": _env_float("RETRY_BACKOFF_SECONDS", 5.0, minimum=0.0),
        "RETRY_MAX_DELAY_SECONDS": _env_float("RETRY_MAX_DELAY_SECONDS", 120.0, minimum=0.0),

        # Backpressure
        "QUEUE_DEPTH_LIMIT": _env_int("QUEUE_DEPTH_LIMIT", 25, minimum=1),
        "QUEUE_POLL_INTERVAL": _env_float("QUEUE_POLL_INTERVAL", 15.0, minimum=1.0),
        "QUEUE_DRAIN_TIMEOUT": _env_int("QUEUE_DRAIN_TIMEOUT", 1800, minimum=0),

        # Pacing
        "MAX_UPLOADS_PER_RUN": _env_int("MAX_UPLOADS_PER_RUN", 200, minimum=0),
        "UPLOAD_DELAY_SECONDS": _env_float("UPLOAD_DELAY_SECONDS", 5.0, minimum=0.0),

        # Local state
        "STATE_FILE": os.getenv("STATE_FILE", os.path.join(LOG_DIR, "import_state.json")),

        # Optional end-of-run drain (off by default: backpressure already keeps
        # the queue short, and pinning the pod open for an hour is exactly the
        # behaviour this job is trying to avoid).
        "WAIT_FOR_QUEUE_ON_FINISH": _env_bool("WAIT_FOR_QUEUE_ON_FINISH", False),
        "QUEUE_WAIT_TIMEOUT": _env_int("QUEUE_WAIT_TIMEOUT", 3600, minimum=0),
    }

    # Validate required configuration
    required_configs = ["PAPERLESS_API_URL", "PAPERLESS_API_TOKEN"]
    missing_configs = [key for key in required_configs if not config[key]]

    if missing_configs:
        raise ValueError(f"Missing required environment variables: {missing_configs}")

    # The API token is attached to every request, so refuse to ship it over
    # cleartext unless the operator has explicitly opted in for a trusted LAN.
    config["PAPERLESS_API_URL"] = config["PAPERLESS_API_URL"].rstrip("/")
    scheme = urlparse(config["PAPERLESS_API_URL"]).scheme
    allow_insecure = os.getenv("PAPERLESS_ALLOW_INSECURE_HTTP", "").lower() in ("1", "true", "yes")
    if scheme != "https" and not allow_insecure:
        raise ValueError(
            f"PAPERLESS_API_URL must use https (got '{scheme or 'none'}' scheme); "
            "set PAPERLESS_ALLOW_INSECURE_HTTP=true to override on a trusted network"
        )

    return config

# Load configuration
try:
    config = get_container_config()
except ValueError as e:
    print(f"Configuration error: {e}")
    sys.exit(2)

# Set configuration variables
WATCH_DIR = config["WATCH_DIR"]
BASE_API_URL = config["PAPERLESS_API_URL"]
PAPERLESS_API_TOKEN = config["PAPERLESS_API_TOKEN"]
IGNORED_FOLDERS = [folder.strip() for folder in config["IGNORED_FOLDERS"] if folder.strip()]
IGNORED_EXTENSIONS = [ext.strip() for ext in config["IGNORED_EXTENSIONS"] if ext.strip()]
IGNORED_PATHS = [path.strip() for path in config["IGNORED_PATHS"] if path.strip()]

PREFLIGHT_ENABLED = config["PREFLIGHT_ENABLED"]
MAX_CONSECUTIVE_FAILURES = config["MAX_CONSECUTIVE_FAILURES"]
MAX_RETRIES = config["MAX_RETRIES"]
RETRY_BACKOFF_SECONDS = config["RETRY_BACKOFF_SECONDS"]
RETRY_MAX_DELAY_SECONDS = config["RETRY_MAX_DELAY_SECONDS"]
QUEUE_DEPTH_LIMIT = config["QUEUE_DEPTH_LIMIT"]
QUEUE_POLL_INTERVAL = config["QUEUE_POLL_INTERVAL"]
QUEUE_DRAIN_TIMEOUT = config["QUEUE_DRAIN_TIMEOUT"]
MAX_UPLOADS_PER_RUN = config["MAX_UPLOADS_PER_RUN"]
UPLOAD_DELAY_SECONDS = config["UPLOAD_DELAY_SECONDS"]
STATE_FILE = config["STATE_FILE"]
WAIT_FOR_QUEUE_ON_FINISH = config["WAIT_FOR_QUEUE_ON_FINISH"]
QUEUE_WAIT_TIMEOUT = config["QUEUE_WAIT_TIMEOUT"]

# Global variables
submitted_tasks = {}

# Backpressure needs to read /api/tasks/, which not every token is allowed to
# do. Set to the server's verdict the first time it refuses, which switches
# backpressure off for the rest of the run instead of re-asking per upload.
tasks_endpoint_denied = None
queue_depth_warned = False

# ----------------------------
# Logging Configuration
# ----------------------------
def setup_logging():
    """Setup logging to file with rotation"""
    # Create logs directory if it doesn't exist
    log_dir = LOG_DIR
    os.makedirs(log_dir, exist_ok=True)

    # Generate log filename with current date
    log_filename = f"paperless_import_{datetime.datetime.now().strftime('%Y%m%d')}.log"
    log_path = os.path.join(log_dir, log_filename)

    # Clean up old log files
    cleanup_old_logs(log_dir)

    file_handler = logging.FileHandler(log_path, encoding='utf-8')

    # Log files can contain document filenames, so keep them owner-readable only.
    # Best-effort: the log directory is often a bind mount this user does not own,
    # and tightening permissions is not worth failing the whole run over.
    for target, mode in ((log_dir, 0o700), (log_path, 0o600)):
        try:
            os.chmod(target, mode)
        except OSError as e:
            print(f"Warning: could not restrict permissions on {target}: {e}")

    # Configure logging - in containers, also log to stdout
    handlers = [
        file_handler,
        logging.StreamHandler(sys.stdout)  # For Kubernetes logging
    ]

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers
    )

    return logging.getLogger(__name__)

def cleanup_old_logs(log_dir):
    """Remove log files older than configured retention days"""
    try:
        cutoff_date = datetime.datetime.now() - datetime.timedelta(days=config["LOG_RETENTION_DAYS"])
        log_pattern = os.path.join(log_dir, "paperless_import_*.log")

        for log_file in glob.glob(log_pattern):
            try:
                file_time = datetime.datetime.fromtimestamp(os.path.getctime(log_file))
                if file_time < cutoff_date:
                    os.remove(log_file)
                    print(f"Removed old log file: {os.path.basename(log_file)}")
            except Exception as e:
                print(f"Warning: Could not process log file {log_file}: {e}")
    except Exception as e:
        print(f"Warning: Could not clean up old logs: {e}")

# Global variables for error tracking
logger = None
has_errors = False
has_critical_errors = False

def redact(message):
    """Strip the API token from anything headed for a log sink.

    Logs go to stdout (collected cluster-wide) and to disk, so a token echoed
    back in an error body or a formatted URL must never survive to either.
    """
    if PAPERLESS_API_TOKEN:
        message = message.replace(PAPERLESS_API_TOKEN, "***REDACTED***")
    return message

def log_message(message, level="INFO"):
    """Print messages with a timestamp and log to file."""
    global has_errors, has_critical_errors

    if level == "ERROR":
        has_errors = True
    elif level == "CRITICAL":
        has_critical_errors = True

    # Clean message for file logging (remove emojis for container logs)
    clean_message = redact(message)
    emojis_to_remove = ["❌", "⚠️", "✅", "📨", "📄", "🔍", "⏳", "📊", "🚫", "⏭️", "📁", "🧹", "ℹ️", "🚀"]
    for emoji in emojis_to_remove:
        clean_message = clean_message.replace(emoji, "").strip()

    # Log to file if logger is available (without emojis)
    if logger:
        if level == "ERROR":
            logger.error(clean_message)
        elif level == "CRITICAL":
            logger.critical(clean_message)
        elif level == "WARNING":
            logger.warning(clean_message)
        else:
            logger.info(clean_message)

def summarize_body(response, limit=200):
    """Condense a response body for logging.

    A 503 from an ingress is often a full HTML error page; thousands of those
    turn the run log into noise nobody reads.
    """
    if response is None:
        return "no response"
    body = " ".join((response.text or "").split())
    if len(body) > limit:
        body = body[:limit] + "…"
    return body or f"HTTP {response.status_code}"

# ----------------------------
# Initialize API Headers
# ----------------------------
HEADERS = {
    "Authorization": f"Token {PAPERLESS_API_TOKEN}",
    "Accept": "application/json"
}

# ----------------------------
# Circuit Breaker
# ----------------------------
class CircuitBreakerOpen(RuntimeError):
    """Raised once the backend has failed too many times in a row.

    Nothing catches this below main(): tripping it must unwind the whole run.
    """


class CircuitBreaker:
    """Counts consecutive backend failures and aborts the run past a threshold.

    A "failure" is one logical API call that exhausted its retries — either a
    connection error or a 5xx/429. Any successful call resets the count, so a
    single bad document cannot trip it, but an absent backend trips it fast.
    """

    def __init__(self, threshold):
        self.threshold = threshold
        self.consecutive = 0

    def reset(self):
        self.consecutive = 0

    def record_success(self):
        self.consecutive = 0

    def record_failure(self, reason):
        self.consecutive += 1
        if self.consecutive >= self.threshold:
            raise CircuitBreakerOpen(
                f"{self.consecutive} consecutive backend failures "
                f"(threshold {self.threshold}); last: {reason}"
            )


breaker = CircuitBreaker(MAX_CONSECUTIVE_FAILURES)

# ----------------------------
# HTTP layer
# ----------------------------
def retry_after_seconds(response):
    """Honour a server-supplied Retry-After, clamped to our own ceiling."""
    if response is None:
        return None
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        # The header may also be an HTTP-date; parse it rather than guessing.
        try:
            target = parsedate_to_datetime(raw.strip())
        except (TypeError, ValueError):
            return None
        if target is None:
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=datetime.timezone.utc)
        seconds = (target - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    return max(0.0, min(seconds, RETRY_MAX_DELAY_SECONDS))


def api_request(method, path, *, timeout=REQUEST_TIMEOUT, retries=None, upload_path=None, **kwargs):
    """Make one Paperless API call with bounded retries and breaker accounting.

    Every call in this script goes through here, which is what makes the
    failure policy uniform: transient statuses (429/503/…) and connection
    errors are retried with exponential backoff, and the *outcome* — not each
    individual attempt — is what the circuit breaker counts.

    Returns the final ``Response`` (which may itself be an error response), or
    ``None`` if every attempt failed at the connection level. Raises
    ``CircuitBreakerOpen`` when the backend has failed too many times in a row.
    """
    attempts = (MAX_RETRIES if retries is None else retries) + 1
    delay = RETRY_BACKOFF_SECONDS
    url = f"{BASE_API_URL}{path}"
    response = None
    reason = "unknown error"

    for attempt in range(1, attempts + 1):
        response = None
        try:
            if upload_path is None:
                response = requests.request(method, url, headers=HEADERS, timeout=timeout, **kwargs)
            else:
                # Reopen the file for every attempt: a retried upload must not
                # re-send an already-consumed stream.
                with open(upload_path, "rb") as handle:
                    files = {"document": (os.path.basename(upload_path), handle)}
                    response = requests.request(
                        method, url, headers=HEADERS, timeout=timeout, files=files, **kwargs
                    )
        except requests.exceptions.RequestException as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code not in RETRYABLE_STATUSES and response.status_code < 500:
                breaker.record_success()
                return response
            reason = f"HTTP {response.status_code}"

        if attempt == attempts:
            break

        wait = retry_after_seconds(response)
        if wait is None:
            wait = delay
            delay = min(delay * 2, RETRY_MAX_DELAY_SECONDS)
        log_message(
            f"{method} {path}: {reason}; retry {attempt}/{attempts - 1} in {wait:.0f}s",
            "WARNING",
        )
        time.sleep(wait)

    breaker.record_failure(reason)
    log_message(f"{method} {path} failed after {attempts} attempt(s): {reason}", "WARNING")
    return response


def response_json(response):
    """Decode a JSON body, tolerating a proxy that returned HTML."""
    if response is None:
        return None
    try:
        return response.json()
    except ValueError:
        log_message(f"Unparseable JSON response: {summarize_body(response)}", "WARNING")
        return None

# ----------------------------
# Preflight
# ----------------------------
def preflight_probe():
    """Confirm the token works against an endpoint every user may read.

    Used when /api/status/ is unusable. It gives no health detail, only the
    reachable/authenticated distinction, which is all the caller needs to
    decide between "keep going" and "come back later".
    """
    try:
        response = api_request("GET", "/ui_settings/", retries=1)
    except CircuitBreakerOpen as exc:
        log_message(f"Preflight probe failed: {exc}", "WARNING")
        return "unavailable"
    finally:
        breaker.reset()

    if response is None:
        return "unavailable"

    if response.status_code in (401, 403):
        return "unauthorized"

    if response.status_code != 200:
        log_message(
            f"Preflight probe: HTTP {response.status_code} — {summarize_body(response)}",
            "WARNING",
        )
        return "unavailable"

    return "ok"


def preflight_check():
    """Ask Paperless once whether it is up, before touching the file tree.

    Returns one of "ok", "unavailable" or "unauthorized". Walking 6000 files
    and failing every upload is a pointless way to discover that the stack is
    scaled to zero, so a single cheap request gates the entire run.
    """
    try:
        response = api_request("GET", "/status/", retries=1)
    except CircuitBreakerOpen as exc:
        log_message(f"Preflight failed: {exc}", "WARNING")
        return "unavailable"
    finally:
        # Preflight failures are reported on their own terms; they must not
        # count towards the budget for the run that follows.
        breaker.reset()

    if response is None:
        return "unavailable"

    if response.status_code == 401:
        return "unauthorized"

    if response.status_code == 403:
        # /api/status/ is superuser-only. A perfectly good import token gets a
        # 403 here, so this says nothing about the token — re-ask somewhere any
        # authenticated user may look before condemning it.
        log_message("/api/status/ is not readable by this token; probing /api/ui_settings/ instead")
        return preflight_probe()

    if response.status_code == 404:
        # /api/status/ arrived in Paperless 2.x. On anything older, reaching the
        # API at all is the only health signal available.
        log_message("/api/status/ not available on this server; skipping health details")
        return "ok"

    if response.status_code != 200:
        log_message(f"Preflight: HTTP {response.status_code} — {summarize_body(response)}", "WARNING")
        return "unavailable"

    data = response_json(response)
    if not isinstance(data, dict):
        return "unavailable"

    problems = []
    database_status = (data.get("database") or {}).get("status")
    if database_status and database_status.upper() != "OK":
        problems.append(f"database={database_status}")

    tasks = data.get("tasks") or {}
    for field in ("redis_status", "celery_status"):
        value = tasks.get(field)
        if value and value.upper() != "OK":
            problems.append(f"{field}={value}")

    if problems:
        log_message(f"Paperless reports unhealthy subsystems: {', '.join(problems)}", "WARNING")
        return "unavailable"

    log_message(
        f"Preflight OK — Paperless {data.get('pngx_version', 'unknown')} "
        f"({data.get('install_type', 'unknown')})"
    )
    return "ok"

# ----------------------------
# Local State
# ----------------------------
class ImportState:
    """Durable record of what this importer has already dealt with.

    Deduplication used to require a live API, so an outage made every file look
    new and the next run re-submitted the lot. Keeping the answer locally means
    a run that is capped, interrupted or facing a dead backend resumes exactly
    where it stopped.

    Entries are keyed by absolute path and carry size + mtime + checksum: the
    stat pair is the cheap fast path, the checksum catches files that moved or
    were renamed.
    """

    def __init__(self, path):
        self.path = path
        self.entries = {}
        self.by_checksum = {}
        self.dirty = 0
        self.writable = True

    def load(self):
        if not self.path:
            self.writable = False
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            log_message(f"No state file yet at {self.path}; starting fresh")
            return
        except (OSError, ValueError) as exc:
            log_message(f"Ignoring unreadable state file {self.path}: {exc}", "WARNING")
            return

        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            log_message(f"Ignoring state file with unexpected format: {self.path}", "WARNING")
            return

        entries = data.get("documents")
        if isinstance(entries, dict):
            self.entries = entries
            self._reindex()
        log_message(f"Loaded {len(self.entries)} entries from state file")

    def _reindex(self):
        self.by_checksum = {
            entry["checksum"]: path
            for path, entry in self.entries.items()
            if isinstance(entry, dict) and entry.get("checksum")
        }

    def lookup(self, file_path, size, mtime):
        """Return the recorded entry if this exact file was already handled."""
        entry = self.entries.get(file_path)
        if not isinstance(entry, dict):
            return None
        if entry.get("size") != size:
            return None
        # mtimes cross filesystems and JSON round-trips, so compare loosely.
        recorded_mtime = entry.get("mtime")
        if recorded_mtime is None or abs(float(recorded_mtime) - mtime) > 1:
            return None
        return entry

    def lookup_checksum(self, checksum):
        """Return the path this checksum was recorded under, if any."""
        path = self.by_checksum.get(checksum)
        if path is None:
            return None
        return self.entries.get(path)

    def record(self, file_path, size, mtime, checksum, status, task_id=None):
        entry = {
            "size": size,
            "mtime": mtime,
            "checksum": checksum,
            "status": status,
            "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        if task_id:
            entry["task_id"] = task_id
        self.entries[file_path] = entry
        if checksum:
            self.by_checksum[checksum] = file_path
        self.dirty += 1

    def prune(self, seen_paths):
        """Drop entries for files that have disappeared from the watch tree.

        Skipped when the walk found nothing: an unmounted share must not be
        mistaken for "the user deleted everything".
        """
        if not seen_paths:
            return 0
        stale = [path for path in self.entries if path not in seen_paths and not os.path.exists(path)]
        for path in stale:
            self.entries.pop(path, None)
        if stale:
            self._reindex()
            self.dirty += len(stale)
        return len(stale)

    def save(self, force=False):
        """Persist the state, atomically. Never fatal: logs live here too.

        Unforced saves batch up, so scanning thousands of files does not mean
        thousands of rewrites. Submissions always force a flush: that is the
        write an interrupted run cannot afford to lose.
        """
        if not self.path or not self.writable:
            return
        if not force and self.dirty < SAVE_BATCH_SIZE:
            return

        payload = {
            "version": STATE_VERSION,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "documents": self.entries,
        }
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        tmp_path = None
        try:
            os.makedirs(directory, exist_ok=True)
            # Same directory as the target, so os.replace stays atomic.
            fd, tmp_path = tempfile.mkstemp(prefix=".import_state.", dir=directory)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            tmp_path = None
            self.dirty = 0
        except OSError as exc:
            log_message(f"Could not write state file {self.path}: {exc}", "WARNING")
            self.writable = False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

# ----------------------------
# Function to Calculate File Checksum
# ----------------------------
def calculate_file_checksum(file_path):
    """Calculate MD5 checksum of a file.

    MD5 is not a security control here: Paperless-ngx stores document checksums
    as MD5, so we must match its algorithm to query for duplicates. Flagged
    accordingly so scanners and FIPS-enabled hosts do not treat it as crypto.
    """
    hash_md5 = hashlib.md5(usedforsecurity=False)
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        log_message(f"Error calculating checksum for {file_path}: {e}", "WARNING")
        return None

# ----------------------------
# Function to Check if Document Already Exists
# ----------------------------
def document_exists(file_path, checksum):
    """Check if document already exists in Paperless-ngx by checksum and filename."""
    filename = os.path.basename(file_path)

    if not checksum:
        log_message(f"Could not calculate checksum for {filename}, skipping existence check", "WARNING")
        return False

    # Check by checksum first (most reliable) - limit to 1 result for efficiency
    response = api_request(
        "GET", "/documents/", params={"checksum__iexact": checksum, "page_size": 1}
    )
    if response is not None and response.status_code == 200:
        data = response_json(response) or {}
        if data.get("count", 0) > 0:
            existing_doc = data["results"][0]
            log_message(f"Document already exists (checksum match): {filename} -> '{existing_doc.get('title', 'Unknown')}'")
            return True
    elif response is not None:
        log_message(f"Error checking document by checksum: HTTP {response.status_code}", "WARNING")

    # Fallback: check by exact filename - limit to 1 result for efficiency
    response = api_request(
        "GET", "/documents/", params={"original_filename__iexact": filename, "page_size": 1}
    )
    if response is not None and response.status_code == 200:
        data = response_json(response) or {}
        if data.get("count", 0) > 0:
            existing_doc = data["results"][0]
            log_message(f"Document already exists (filename match): {filename} -> '{existing_doc.get('title', 'Unknown')}'")
            return True
    elif response is not None:
        log_message(f"Error checking document by filename: HTTP {response.status_code}", "WARNING")

    return False

# ----------------------------
# Retrieve Existing Tags from Paperless
# ----------------------------
def next_page_path(next_url):
    """Reduce a paginated ``next`` URL to a path this script can request again.

    Django builds that URL from whatever scheme and host the reverse proxy
    advertised, which need not match PAPERLESS_API_URL character for character
    (http vs https, internal vs external hostname). Comparing the whole prefix
    therefore drops pages on the floor; only the path below the API root is
    meaningful.
    """
    if not next_url:
        return None

    base_path = urlparse(BASE_API_URL).path.rstrip("/")
    parsed = urlparse(next_url)
    path = parsed.path

    if base_path:
        if not path.startswith(base_path):
            log_message(f"Unexpected pagination URL '{next_url}'", "WARNING")
            return None
        path = path[len(base_path):]

    return f"{path}?{parsed.query}" if parsed.query else path


def get_existing_tags():
    """Retrieve all existing tags from Paperless-NGX, handling pagination.

    Returns ``None`` if the listing could not be read in full. A partial cache
    is worse than no cache: every unseen tag looks like a missing one, so the
    run re-creates it (which the server rejects on the name unique constraint)
    and then uploads documents with that tag silently dropped.
    """
    all_tags = {}
    seen = 0
    expected = None
    path = "/tags/"
    params = {"page_size": 100}

    while path:
        response = api_request("GET", path, params=params)
        params = None  # subsequent pages carry their own query string

        if response is None or response.status_code != 200:
            log_message(f"Failed to fetch tags: {summarize_body(response)}", "ERROR")
            return None

        data = response_json(response)
        if data is None:
            return None

        if expected is None:
            expected = data.get("count")

        results = data.get("results", [])
        seen += len(results)
        for tag in results:
            all_tags[tag["name"].lower()] = tag["id"]

        path = next_page_path(data.get("next"))

    if expected is not None and seen < expected:
        log_message(f"Tag listing truncated: read {seen} of {expected} tag(s)", "ERROR")
        return None

    log_message(f"Retrieved {len(all_tags)} tags from Paperless-NGX.")
    return all_tags

# ----------------------------
# Handle Document Tagging
# ----------------------------
def tag_names_from_path(file_path):
    """Derive tag names from the folder structure. Pure — makes no API calls.

    Keeping this side-effect free is the point: tags used to be created while
    walking each file's path, so a failed upload left orphan tags behind and
    every subsequent file re-attempted the same creations.
    """
    normalized_path = os.path.normpath(file_path)

    for ignored in IGNORED_PATHS:
        if normalized_path.startswith(os.path.normpath(ignored)):
            normalized_path = normalized_path[len(os.path.normpath(ignored)):]
            break

    parent_directory = os.path.dirname(normalized_path)
    names = []

    for folder in parent_directory.split(os.sep):
        folder = folder.strip()
        if not folder or folder.lower() in [ignored.lower() for ignored in IGNORED_FOLDERS]:
            continue

        for sub_tag in folder.split():
            sub_tag = sub_tag.lower().strip()
            # Paperless caps tag names at 128 characters.
            if sub_tag and len(sub_tag) <= 128 and sub_tag not in names:
                names.append(sub_tag)

    return names


def lookup_tag(name):
    """Ask the server for one tag by name, returning its id or ``None``."""
    response = api_request("GET", "/tags/", params={"name__iexact": name, "page_size": 100})
    if response is None or response.status_code != 200:
        return None

    data = response_json(response) or {}
    for tag in data.get("results", []):
        if tag.get("name", "").lower() == name.lower():
            return tag.get("id")
    return None


def ensure_tags(names, tag_cache):
    """Create every missing tag once per run, before any upload.

    Resolving the whole tag set up front means a document is never submitted
    with a half-built tag list, and a tag is never created more than once no
    matter how many files share a folder.
    """
    missing = [name for name in sorted(names) if name not in tag_cache]
    if not missing:
        return

    log_message(f"Creating {len(missing)} missing tag(s)")
    created = 0
    adopted = 0
    unresolved = []
    for name in missing:
        response = api_request("POST", "/tags/", json={"name": name})
        if response is not None and response.status_code in (200, 201):
            data = response_json(response) or {}
            if "id" in data:
                tag_cache[name] = data["id"]
                created += 1
                continue

        # A rejected creation is usually the name unique constraint: the tag
        # exists but was absent from the listing we cached. Adopt the server's
        # copy rather than dropping the tag from every document that wants it.
        existing_id = lookup_tag(name) if response is not None and response.status_code == 400 else None
        if existing_id is not None:
            tag_cache[name] = existing_id
            adopted += 1
            continue

        unresolved.append(name)
        log_message(f"Failed to create tag '{name}': {summarize_body(response)}", "WARNING")

    log_message(f"Created {created}/{len(missing)} tag(s), adopted {adopted} already-existing")
    if unresolved:
        shown = ", ".join(unresolved[:10]) + ("…" if len(unresolved) > 10 else "")
        log_message(
            f"{len(unresolved)} tag(s) unresolved; documents will be uploaded without them: {shown}",
            "ERROR",
        )


def resolve_tag_ids(names, tag_cache):
    """Map tag names to IDs, silently dropping any the server would not create."""
    return [tag_cache[name] for name in names if name in tag_cache]

# ----------------------------
# Backpressure
# ----------------------------
def note_tasks_denied(response):
    """Record that this token may not read /api/tasks/, if that is the verdict.

    A 401/403/404 there is permanent for the run: the endpoint is restricted
    (or absent) and will not become readable between two uploads. Remembering
    the verdict is what stops us paying two rejected calls before every single
    document. Returns True when the response was such a verdict.
    """
    global tasks_endpoint_denied

    if response is None or response.status_code not in (401, 403, 404):
        return False

    if tasks_endpoint_denied is None:
        tasks_endpoint_denied = f"HTTP {response.status_code} — {summarize_body(response)}"
    return True


def get_queue_depth():
    """Count the consume tasks Paperless has queued or in flight.

    Returns None if the depth cannot be determined.
    """
    total = 0
    for status in QUEUE_ACTIVE_STATUSES:
        response = api_request("GET", "/tasks/", params={"status": status})
        if response is None:
            return None
        if response.status_code != 200:
            if not note_tasks_denied(response):
                log_message(
                    f"GET /tasks/?status={status}: HTTP {response.status_code} — "
                    f"{summarize_body(response)}",
                    "WARNING",
                )
            return None
        data = response_json(response)
        if isinstance(data, list):
            total += len(data)
        elif isinstance(data, dict):
            total += data.get("count", len(data.get("results", [])))
        else:
            return None
    return total


def wait_for_queue_capacity():
    """Block until Paperless has room for another document.

    This is the self-limiting bit: however many files the walk turns up, we
    only ever hand the cluster more OCR work once it has retired the last
    batch. Returns False if the queue never drained within the budget.
    """
    global queue_depth_warned

    # Already established that we cannot see the queue: no point re-asking
    # before every upload, and no point repeating the warning either.
    if tasks_endpoint_denied is not None:
        return True

    deadline = time.time() + QUEUE_DRAIN_TIMEOUT
    announced = False

    while True:
        depth = get_queue_depth()

        if depth is None:
            if tasks_endpoint_denied is not None:
                log_message(
                    "Backpressure is OFF for this run: this API token may not read "
                    f"/api/tasks/ ({tasks_endpoint_denied}). Uploads are paced by "
                    f"UPLOAD_DELAY_SECONDS={UPLOAD_DELAY_SECONDS:.0f}s and "
                    f"MAX_UPLOADS_PER_RUN={MAX_UPLOADS_PER_RUN or 'unlimited'} only — "
                    "size those for what the cluster can absorb, or grant the token "
                    "permission to view tasks",
                    "WARNING",
                )
            elif not queue_depth_warned:
                log_message(
                    "Could not read task queue depth; proceeding without backpressure "
                    "(further occurrences this run are not repeated)",
                    "WARNING",
                )
                queue_depth_warned = True
            return True

        if depth <= QUEUE_DEPTH_LIMIT:
            if announced:
                log_message(f"Queue drained to {depth}; resuming uploads")
            return True

        if time.time() >= deadline:
            log_message(
                f"Task queue still at {depth} (limit {QUEUE_DEPTH_LIMIT}) after "
                f"{QUEUE_DRAIN_TIMEOUT}s; stopping this run, the rest will follow next time",
                "WARNING",
            )
            return False

        if not announced:
            log_message(
                f"⏳ Task queue depth {depth} exceeds limit {QUEUE_DEPTH_LIMIT}; "
                f"pausing uploads (re-polling every {QUEUE_POLL_INTERVAL:.0f}s)"
            )
            announced = True

        time.sleep(QUEUE_POLL_INTERVAL)

# ----------------------------
# Upload Documents
# ----------------------------
def upload_document(file_path, tag_ids):
    """Upload one file to Paperless and record its task ID.

    Returns True on submission, False on failure, None when Paperless declined
    the file for a reason that will not change on a retry.
    """
    data = {"title": os.path.basename(file_path), "tags": tag_ids}

    try:
        response = api_request(
            "POST",
            "/documents/post_document/",
            timeout=UPLOAD_TIMEOUT,
            upload_path=file_path,
            data=data,
        )
    except OSError as exc:
        log_message(f"Could not read '{os.path.basename(file_path)}': {exc}", "ERROR")
        return False

    if response is None:
        log_message(f"Error submitting document '{os.path.basename(file_path)}': no response", "ERROR")
        return False

    if response.status_code == 200:
        task_id = response.text.strip().replace('"', '')
        submitted_tasks[task_id] = file_path
        log_message(f"Document '{os.path.basename(file_path)}' submitted (Task UUID: {task_id})")
        return task_id or True

    error_text = (response.text or "").lower()
    # Expected rejections (unsupported file type, empty file, known duplicate)
    # are not errors: they will never succeed, so record and move on.
    if "not supported" in error_text or "empty" in error_text or "duplicate" in error_text:
        log_message(f"Skipping document '{os.path.basename(file_path)}': {summarize_body(response)}", "WARNING")
        return None

    log_message(
        f"Error submitting document '{os.path.basename(file_path)}': {summarize_body(response)}",
        "ERROR",
    )
    return False

# ----------------------------
# Processing Queue and Task Status
# ----------------------------
def get_task_details(task_id):
    """Get detailed information about a specific task"""
    response = api_request("GET", f"/tasks/{task_id}/")
    if response is not None and response.status_code == 200:
        return response_json(response)
    if response is not None:
        log_message(f"Error getting task details for {task_id}: HTTP {response.status_code}", "WARNING")
    return None

def delete_task(task_id):
    """Acknowledge (effectively delete) a specific task by its ID"""
    response = api_request(
        "POST",
        "/tasks/acknowledge/",
        json={"tasks": [int(task_id)]},  # API expects an array of integers
    )

    if response is not None and response.status_code in (200, 204):
        log_message(f"Successfully acknowledged task: {task_id}")
        return True

    log_message(f"Failed to acknowledge task {task_id}: {summarize_body(response)}", "WARNING")
    return False


def list_tasks():
    """Fetch the current task list, normalising the paginated and plain forms."""
    response = api_request("GET", "/tasks/")
    if response is None or response.status_code != 200:
        note_tasks_denied(response)
        log_message(f"Failed to fetch tasks: {summarize_body(response)}", "WARNING")
        return None
    data = response_json(response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("results", [])
    return None


def clear_all_tasks():
    """Clear all tasks from the queue"""
    log_message("🧹 Clearing all tasks from the queue due to stuck tasks...", "WARNING")
    tasks = list_tasks()
    if tasks is None:
        log_message("Failed to get tasks for clearing", "ERROR")
        return False

    task_ids_to_delete = [task.get("id") for task in tasks if task.get("id")]

    if not task_ids_to_delete:
        log_message("No tasks found to clear")
        return True

    log_message(f"Found {len(task_ids_to_delete)} tasks to clear")
    deleted_count = 0
    failed_count = 0

    for task_id in task_ids_to_delete:
        if delete_task(task_id):
            deleted_count += 1
        else:
            failed_count += 1

    if failed_count == 0:
        log_message(f"✅ Successfully cleared {deleted_count} tasks from the queue")
        return True

    log_message(f"⚠️ Cleared {deleted_count} tasks, but {failed_count} tasks failed to delete", "WARNING")
    return False

def acknowledge_completed_tasks():
    """Acknowledge all completed tasks to clean up the queue"""
    tasks = list_tasks()
    if tasks is None:
        return

    completed_task_ids = [
        task.get("id") for task in tasks
        if task.get("status") in ["SUCCESS", "FAILURE"] and not task.get("acknowledged", False)
    ]

    if not completed_task_ids:
        log_message("No completed tasks to acknowledge")
        return

    log_message(f"Acknowledging {len(completed_task_ids)} completed tasks...")
    response = api_request("POST", "/tasks/acknowledge/", json={"tasks": completed_task_ids})
    if response is not None and response.status_code in (200, 204):
        log_message(f"Successfully acknowledged {len(completed_task_ids)} completed tasks")
    else:
        log_message(f"Failed to acknowledge tasks: {summarize_body(response)}", "WARNING")

def wait_for_queue_to_clear():
    """Wait until the Paperless task queue is empty before checking task statuses"""
    log_message("⏳ Waiting for the Paperless queue to clear...")

    stuck_task_threshold = 300  # If a task stays the same for 300 seconds (5 minutes), consider it stuck
    task_stuck_timer = {}
    queue_cleared_due_to_stuck_tasks = False
    # Hard deadline so an unreachable or permanently wedged queue cannot pin this
    # job open indefinitely.
    deadline = time.time() + QUEUE_WAIT_TIMEOUT

    while time.time() < deadline:
        tasks = list_tasks()
        if tasks is None:
            if tasks_endpoint_denied is not None:
                log_message(
                    "Cannot watch the queue with this token "
                    f"({tasks_endpoint_denied}); ending the run without draining"
                )
                return
            time.sleep(QUEUE_POLL_INTERVAL)
            continue

        # Based on the API spec, these are the active statuses that should be waited for
        active_statuses = ["PENDING", "RECEIVED", "STARTED", "RETRY"]
        active_tasks = [task for task in tasks if task.get("status") in active_statuses]

        log_message(f"📊 Active tasks in queue: {len(active_tasks)}")

        if active_tasks:
            current_task_ids = []
            current_time = time.time()
            has_stuck_tasks = False

            for task in active_tasks:
                task_id = task.get("task_id", "Unknown")
                status = task.get("status", "Unknown")
                task_name = task.get("task_name", "Unknown")
                current_task_ids.append(task_id)

                # Track how long this task has been stuck
                if task_id not in task_stuck_timer:
                    task_stuck_timer[task_id] = current_time
                else:
                    stuck_duration = current_time - task_stuck_timer[task_id]
                    if stuck_duration > stuck_task_threshold:
                        log_message(f"   - Task {task_id}: {status} ({task_name}) STUCK for {int(stuck_duration)}s", "WARNING")
                        has_stuck_tasks = True

            # If we have tasks stuck for more than 5 minutes, clear the entire queue
            if has_stuck_tasks and not queue_cleared_due_to_stuck_tasks:
                log_message("❌ Detected tasks stuck for more than 5 minutes. Clearing the entire queue...", "WARNING")
                if clear_all_tasks():
                    queue_cleared_due_to_stuck_tasks = True
                    task_stuck_timer.clear()
                    log_message("🚀 Queue cleared successfully. Continuing to monitor...")
                    # Continue monitoring to ensure the queue is actually clear
                else:
                    log_message("⚠️ Failed to clear the queue completely. Will retry on next iteration.", "ERROR")

            # Clean up timers for tasks that are no longer active
            task_stuck_timer = {tid: timer for tid, timer in task_stuck_timer.items() if tid in current_task_ids}
        else:
            if queue_cleared_due_to_stuck_tasks:
                log_message("✅ Task queue is now empty after clearing stuck tasks.")
            else:
                log_message("✅ Task queue is now empty. Proceeding with final status check.")
            return

        time.sleep(QUEUE_POLL_INTERVAL)

    log_message(
        f"Gave up waiting for the task queue after {QUEUE_WAIT_TIMEOUT}s; "
        "some documents may still be processing",
        "ERROR",
    )

# ----------------------------
# Scanning
# ----------------------------
def scan_watch_dir():
    """Walk the watch tree, newest first, collecting (path, size, mtime)."""
    all_files = []
    for root, _, files in os.walk(WATCH_DIR):
        for filename in files:
            full_path = os.path.join(root, filename)
            try:
                stat_result = os.stat(full_path)
            except OSError as exc:
                log_message(f"File not found or inaccessible: {full_path} ({exc})", "WARNING")
                continue
            all_files.append((full_path, stat_result.st_size, stat_result.st_mtime))

    # Sort by descending modification time
    all_files.sort(key=lambda item: item[2], reverse=True)
    return all_files


def select_candidates(all_files, state, counters):
    """Pick the files to upload this run, cheapest checks first.

    Order matters for a slow backend: name-based filters and the local state
    file cost nothing, so the API is only consulted for files that survive
    them. The scan also stops as soon as the per-run cap is met, which is why
    a 6000-file tree no longer means 6000 API round-trips.
    """
    candidates = []

    for file_path, size, mtime in all_files:
        filename = os.path.basename(file_path)
        file_ext = os.path.splitext(filename)[1].lower()

        # Check if file is in ignored folder
        if any(ignored_folder.lower() in file_path.lower() for ignored_folder in IGNORED_FOLDERS):
            counters["skipped_ignored"] += 1
            continue

        # Check if file extension is unsupported
        if file_ext in IGNORED_EXTENSIONS:
            counters["skipped_unsupported"] += 1
            continue

        # Local state: the fast path, and the only one that works offline.
        entry = state.lookup(file_path, size, mtime)
        if entry is not None:
            counters["skipped_state"] += 1
            continue

        # Cap checked here, after the free filters but before the expensive
        # ones: nothing beyond this point happens for files we will not offer
        # to Paperless this run.
        if MAX_UPLOADS_PER_RUN and len(candidates) >= MAX_UPLOADS_PER_RUN:
            counters["deferred"] += 1
            continue

        checksum = calculate_file_checksum(file_path)
        if checksum:
            # Same bytes under a new name or path: already handled.
            entry = state.lookup_checksum(checksum)
            if entry is not None:
                state.record(file_path, size, mtime, checksum, entry.get("status", "submitted"))
                counters["skipped_state"] += 1
                continue

        # Only now is it worth asking Paperless.
        if document_exists(file_path, checksum):
            state.record(file_path, size, mtime, checksum, "exists")
            counters["skipped_existing"] += 1
            state.save()
            continue

        candidates.append((file_path, size, mtime, checksum))

    return candidates

# ----------------------------
# Main Execution
# ----------------------------
def log_run_settings():
    log_message(f"Watch directory: {WATCH_DIR}")
    log_message(f"Paperless API URL: {BASE_API_URL}")
    log_message(
        "Limits: "
        f"max_uploads_per_run={MAX_UPLOADS_PER_RUN or 'unlimited'}, "
        f"queue_depth_limit={QUEUE_DEPTH_LIMIT}, "
        f"upload_delay={UPLOAD_DELAY_SECONDS:.0f}s, "
        f"max_consecutive_failures={MAX_CONSECUTIVE_FAILURES}, "
        f"max_retries={MAX_RETRIES}"
    )


def main():
    global logger

    # Initialize logging
    logger = setup_logging()
    log_message("Starting Paperless-NGX Import Process")
    log_run_settings()

    # Check if watch directory exists
    if not os.path.exists(WATCH_DIR):
        log_message(f"Watch directory does not exist: {WATCH_DIR}", "CRITICAL")
        return 2

    # ---- 1. Preflight: is there anything to talk to? ----
    if PREFLIGHT_ENABLED:
        status = preflight_check()
        if status == "unauthorized":
            log_message("Paperless rejected our API token", "CRITICAL")
            return 2
        if status != "ok":
            log_message(
                "Paperless is not reachable or not healthy; nothing to do this run. "
                "Exiting cleanly — the next run will pick up where this one left off."
            )
            return 0

    state = ImportState(STATE_FILE)
    state.load()

    counters = {
        "skipped_ignored": 0,
        "skipped_unsupported": 0,
        "skipped_state": 0,
        "skipped_existing": 0,
        "skipped_api_issues": 0,
        "deferred": 0,
    }
    uploaded_count = 0
    failed_uploads = 0
    all_files = []
    candidates = []
    aborted = False
    throttled = False

    try:
        tag_cache = get_existing_tags()
        if tag_cache is None:
            # Uploading now would tag nothing correctly and pointlessly retry
            # creating tags that already exist; the next run picks this up.
            log_message("Could not read the existing tags — aborting before any upload", "CRITICAL")
            return 2

        if has_critical_errors:
            log_message("Critical errors encountered during initialization", "CRITICAL")
            return 2

        # ---- 2. Scan and select ----
        all_files = scan_watch_dir()
        log_message(f"Found {len(all_files)} files to process")

        pruned = state.prune({path for path, _, _ in all_files})
        if pruned:
            log_message(f"Pruned {pruned} state entries for files that no longer exist")

        candidates = select_candidates(all_files, state, counters)
        log_message(f"Selected {len(candidates)} document(s) for upload this run")

        # ---- 3. Resolve the whole tag set once, before any upload ----
        wanted_tags = set()
        tag_names_by_path = {}
        for file_path, _, _, _ in candidates:
            names = tag_names_from_path(file_path)
            tag_names_by_path[file_path] = names
            wanted_tags.update(names)
        if wanted_tags:
            ensure_tags(wanted_tags, tag_cache)

        # ---- 4. Upload, paced and backpressured ----
        for index, (file_path, size, mtime, checksum) in enumerate(candidates):
            if not wait_for_queue_capacity():
                throttled = True
                counters["deferred"] += len(candidates) - index
                break

            result = upload_document(file_path, resolve_tag_ids(tag_names_by_path[file_path], tag_cache))

            if result is False:
                failed_uploads += 1
            elif result is None:
                # Permanently unacceptable to Paperless — remember it so we do
                # not re-offer the same file every single day.
                state.record(file_path, size, mtime, checksum, "rejected")
                counters["skipped_api_issues"] += 1
            else:
                task_id = result if isinstance(result, str) else None
                state.record(file_path, size, mtime, checksum, "submitted", task_id=task_id)
                uploaded_count += 1
                # Flush immediately: if this run is killed mid-way, the next one
                # must not re-submit what we have already handed over.
                state.save(force=True)

            if UPLOAD_DELAY_SECONDS and index < len(candidates) - 1:
                time.sleep(UPLOAD_DELAY_SECONDS)

    except CircuitBreakerOpen as exc:
        aborted = True
        log_message(f"❌ Circuit breaker tripped — aborting run: {exc}", "ERROR")
    except Exception as e:
        log_message(f"Unexpected error in main process: {e}", "CRITICAL")
        return 2
    finally:
        state.save(force=True)

    log_message("Processing Summary:")
    log_message(f"   Total files found: {len(all_files)}")
    log_message(f"   Skipped (ignored folders): {counters['skipped_ignored']}")
    log_message(f"   Skipped (unsupported types): {counters['skipped_unsupported']}")
    log_message(f"   Skipped (known from local state): {counters['skipped_state']}")
    log_message(f"   Skipped (already exist): {counters['skipped_existing']}")
    log_message(f"   Skipped (API issues - unsupported/empty): {counters['skipped_api_issues']}")
    log_message(f"   Submitted for upload: {uploaded_count}")
    log_message(f"   Deferred to a later run: {counters['deferred']}")
    if failed_uploads > 0:
        log_message(f"   Failed uploads: {failed_uploads}", "ERROR")

    if counters["deferred"] and not aborted:
        reason = "queue backpressure" if throttled else f"per-run cap ({MAX_UPLOADS_PER_RUN})"
        log_message(f"ℹ️ {counters['deferred']} file(s) deferred by {reason}; the next run continues from here")

    if aborted:
        log_message("Process aborted by the circuit breaker", "ERROR")
        return 1

    if uploaded_count > 0 and WAIT_FOR_QUEUE_ON_FINISH:
        try:
            acknowledge_completed_tasks()
            wait_for_queue_to_clear()
        except CircuitBreakerOpen as exc:
            log_message(f"Circuit breaker tripped while draining the queue: {exc}", "ERROR")
    elif uploaded_count == 0:
        log_message("No new documents to upload!")

    # Determine exit code based on what happened
    if has_critical_errors:
        log_message("Process completed with critical errors", "CRITICAL")
        return 2
    elif has_errors or failed_uploads > 0:
        log_message("Process completed with some errors", "ERROR")
        return 1

    log_message("Process completed successfully")
    return 0

if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
