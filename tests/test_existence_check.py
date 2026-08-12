"""Existence is decided by content, never by name.

The library this importer runs against is organised by folder, so basenames
repeat: /Amazon/HRM 200 Joel/invoice.pdf and /Amazon/Poele Beka Chef/invoice.pdf
are different documents. Matching on the basename declared 33 of them already
imported, and because the verdict is persisted and short-circuits every later
run, each was hidden permanently.
"""

import hashlib

import pytest
from conftest import FakeResponse, write_file


def sha256(content):
    return hashlib.sha256(content).hexdigest()


class FakePaperless:
    """A Paperless that answers both filters the real one supports.

    Answering ``original_filename__iexact`` too is what makes these tests bite:
    a fake that only understood checksums would pass just as happily with the
    basename fallback back in place.
    """

    def __init__(self, documents=()):
        self.by_checksum = {}
        self.by_filename = {}
        for doc_id, content in documents:
            self.by_checksum[sha256(content)] = doc_id
        self.queries = []

    def hold(self, doc_id, content, original_filename):
        self.by_checksum[sha256(content)] = doc_id
        self.by_filename[original_filename.lower()] = doc_id
        return self

    def __call__(self, method, path, **kwargs):
        params = kwargs.get("params") or {}
        self.queries.append((method, path, params))

        if "checksum__iexact" in params:
            doc_id = self.by_checksum.get(params["checksum__iexact"])
        elif "original_filename__iexact" in params:
            doc_id = self.by_filename.get(params["original_filename__iexact"].lower())
        else:
            doc_id = None

        if doc_id is None:
            return FakeResponse(200, {"count": 0, "results": []})
        return FakeResponse(
            200, {"count": 1, "results": [{"id": doc_id, "title": f"doc-{doc_id}"}]}
        )


def test_same_basename_in_another_folder_is_not_mistaken_for_the_same_document(
    importer, monkeypatch, tmp_path, counters
):
    joel = write_file(tmp_path / "Amazon" / "HRM 200 Joel" / "invoice.pdf", b"HRM 200 invoice")
    beka = write_file(tmp_path / "Amazon" / "Poele Beka Chef" / "invoice.pdf", b"Beka Chef invoice")

    # Only Joel's invoice is really in Paperless — under the same basename.
    paperless = FakePaperless().hold(41, b"HRM 200 invoice", "invoice.pdf")
    monkeypatch.setattr(importer, "api_request", paperless)

    state = importer.ImportState(str(tmp_path / "state.json"))
    all_files = [
        (str(path), path.stat().st_size, path.stat().st_mtime) for path in (joel, beka)
    ]

    candidates = importer.select_candidates(all_files, state, counters)

    assert [path for path, _, _, _ in candidates] == [str(beka)]
    assert counters["skipped_existing"] == 1
    assert state.entries[str(joel)]["status"] == "exists"
    assert str(beka) not in state.entries


def test_the_verdict_records_how_it_was_reached(importer, monkeypatch, tmp_path, counters):
    """A skip no future run re-examines has to say what convinced it."""
    doc = write_file(tmp_path / "Amazon" / "invoice.pdf", b"already there")
    monkeypatch.setattr(
        importer, "api_request", FakePaperless(documents=[(7, b"already there")])
    )

    state = importer.ImportState(str(tmp_path / "state.json"))
    importer.select_candidates(
        [(str(doc), doc.stat().st_size, doc.stat().st_mtime)], state, counters
    )

    entry = state.entries[str(doc)]
    assert entry["verified_by"] == "api-checksum"
    assert entry["document_id"] == 7


def test_existence_is_never_queried_by_filename(importer, monkeypatch, tmp_path):
    doc = write_file(tmp_path / "invoice.pdf", b"unknown to paperless")
    paperless = FakePaperless()
    monkeypatch.setattr(importer, "api_request", paperless)

    importer.find_existing_document(str(doc), sha256(b"unknown to paperless"))

    assert paperless.queries, "expected at least one lookup"
    for _, _, params in paperless.queries:
        assert "original_filename__iexact" not in params


def test_a_checksum_hit_is_reported_with_the_matching_document(importer, monkeypatch, tmp_path):
    doc = write_file(tmp_path / "invoice.pdf", b"already there")
    monkeypatch.setattr(
        importer, "api_request", FakePaperless(documents=[(12, b"already there")])
    )

    existing = importer.find_existing_document(str(doc), sha256(b"already there"))

    assert existing["id"] == 12


@pytest.mark.parametrize(
    "response",
    [None, FakeResponse(500, text="upstream exploded"), FakeResponse(403, text="denied")],
    ids=["no-response", "server-error", "forbidden"],
)
def test_a_failed_lookup_offers_the_document_rather_than_skipping_it(
    importer, monkeypatch, tmp_path, response
):
    """Paperless rejects a redundant upload; a wrong "exists" is durable and silent."""
    doc = write_file(tmp_path / "invoice.pdf", b"content")
    monkeypatch.setattr(importer, "api_request", lambda *a, **kw: response)

    assert importer.find_existing_document(str(doc), sha256(b"content")) is None


def test_an_unhashable_file_is_offered_without_any_lookup(importer, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(importer, "api_request", lambda *a, **kw: calls.append(a))

    assert importer.find_existing_document(str(tmp_path / "x.pdf"), None) is None
    assert calls == []


def test_a_checksum_match_elsewhere_in_the_state_file_short_circuits_the_api(
    importer, monkeypatch, tmp_path, counters
):
    """Same bytes under a new path: known locally, so no round-trip."""
    original = write_file(tmp_path / "old" / "invoice.pdf", b"identical bytes")
    moved = write_file(tmp_path / "new" / "renamed.pdf", b"identical bytes")

    state = importer.ImportState(str(tmp_path / "state.json"))
    state.record(
        str(original), original.stat().st_size, original.stat().st_mtime,
        sha256(b"identical bytes"), "exists", verified_by="api-checksum", document_id=3,
    )

    # The importer fixture fails the test on any API call, which is the assertion.
    candidates = importer.select_candidates(
        [(str(moved), moved.stat().st_size, moved.stat().st_mtime)], state, counters
    )

    assert candidates == []
    assert state.entries[str(moved)]["verified_by"] == "state-checksum"
    assert state.entries[str(moved)]["document_id"] == 3
