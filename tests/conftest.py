"""Shared fixtures.

The importer reads its configuration at import time and exits the process if
anything required is missing, so the environment has to be complete before the
first ``import`` — hence the module-level setup here rather than a fixture.
"""

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PAPERLESS_API_URL", "https://paperless.invalid/api")
os.environ.setdefault("PAPERLESS_API_TOKEN", "test-token-not-a-real-one")
os.environ.setdefault("WATCH_DIR", "/mnt/documents")

import import_to_paperless_docker as impl  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
def importer(monkeypatch, tmp_path):
    """The module under test, with the run's global side effects neutralised."""
    monkeypatch.setattr(impl, "WATCH_DIR", "/mnt/documents")
    monkeypatch.setattr(impl, "TAG_TOKENIZER", "legacy")
    monkeypatch.setattr(impl, "IGNORED_PATHS", ["/mnt/"])
    monkeypatch.setattr(impl, "IGNORED_FOLDERS", ["#recycle", "@eaDir"])
    monkeypatch.setattr(impl, "MAX_UPLOADS_PER_RUN", 0)
    monkeypatch.setattr(impl, "has_errors", False)
    monkeypatch.setattr(impl, "has_critical_errors", False)
    # No test may reach the network: anything that tries is a bug in the test.
    monkeypatch.setattr(
        impl, "api_request",
        lambda *a, **kw: pytest.fail(f"unexpected API call: {a} {kw}"),
    )
    return impl


class FakeResponse:
    """Just enough of ``requests.Response`` for the code paths under test."""

    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else ""
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


@pytest.fixture
def counters():
    return {
        "skipped_ignored": 0,
        "skipped_unsupported": 0,
        "skipped_state": 0,
        "skipped_existing": 0,
        "skipped_api_issues": 0,
        "deferred": 0,
    }


def write_file(path, content):
    """Create a file (and its parents) holding ``content``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path
