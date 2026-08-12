"""A state file written by the MD5 era must be upgraded, never silently kept.

Keeping it costs nothing visible and breaks everything quietly: a 32-char
digest matches no SHA256, so every moved or renamed file looks new forever.
"""

import hashlib
import json

from conftest import write_file


def md5(content):
    return hashlib.md5(content, usedforsecurity=False).hexdigest()


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def write_legacy_state(path, documents):
    path.write_text(json.dumps({"version": 1, "documents": documents}), encoding="utf-8")
    return path


def legacy_entry(file_path, content, status="submitted", **extra):
    entry = {
        "size": len(content),
        "mtime": file_path.stat().st_mtime,
        "checksum": md5(content),
        "status": status,
    }
    entry.update(extra)
    return entry


def test_legacy_md5_digests_are_recomputed_as_sha256(importer, tmp_path):
    content = b"the same bytes, moved later"
    doc = write_file(tmp_path / "invoice.pdf", content)
    state_path = write_legacy_state(
        tmp_path / "state.json", {str(doc): legacy_entry(doc, content)}
    )

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    assert state.entries[str(doc)]["checksum"] == sha256(content)
    assert state.version == importer.STATE_VERSION


def test_recomputed_digest_makes_a_moved_file_recognisable(importer, tmp_path):
    """The whole point of the digest: same bytes, new path, already handled."""
    content = b"the same bytes, moved later"
    doc = write_file(tmp_path / "invoice.pdf", content)
    state_path = write_legacy_state(
        tmp_path / "state.json", {str(doc): legacy_entry(doc, content)}
    )

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    assert state.lookup_checksum(sha256(content)) is not None
    # And the digest it replaced no longer resolves to anything.
    assert state.lookup_checksum(md5(content)) is None


def test_legacy_digest_for_a_vanished_file_is_dropped_not_kept(importer, tmp_path):
    gone = tmp_path / "gone.pdf"
    content = b"deleted since"
    entry = {
        "size": len(content),
        "mtime": 1_700_000_000.0,
        "checksum": md5(content),
        "status": "submitted",
    }
    state_path = write_legacy_state(tmp_path / "state.json", {str(gone): entry})

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    # The entry survives — its path still dedups the file if it comes back —
    # but it carries no digest that pretends to match something.
    assert str(gone) in state.entries
    assert "checksum" not in state.entries[str(gone)]
    assert state.by_checksum == {}


def test_changed_file_is_not_rehashed_into_a_false_match(importer, tmp_path):
    """Re-hashing edited bytes would file them under an entry describing the old ones."""
    doc = write_file(tmp_path / "invoice.pdf", b"original content")
    entry = legacy_entry(doc, b"original content")
    doc.write_bytes(b"a longer, different revision of the document")
    state_path = write_legacy_state(tmp_path / "state.json", {str(doc): entry})

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    assert "checksum" not in state.entries[str(doc)]
    assert state.lookup_checksum(sha256(b"a longer, different revision of the document")) is None


def test_unaudited_exists_verdicts_are_revoked(importer, tmp_path):
    """An "exists" verdict from the basename era may be one of the 33 wrong ones."""
    doc = write_file(tmp_path / "invoice.pdf", b"never actually imported")
    state_path = write_legacy_state(
        tmp_path / "state.json",
        {str(doc): legacy_entry(doc, b"never actually imported", status="exists")},
    )

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    assert str(doc) not in state.entries


def test_audited_exists_verdicts_survive(importer, tmp_path):
    doc = write_file(tmp_path / "invoice.pdf", b"genuinely imported")
    entry = legacy_entry(
        doc, b"genuinely imported", status="exists", verified_by="api-checksum", document_id=7
    )
    state_path = write_legacy_state(tmp_path / "state.json", {str(doc): entry})

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    assert state.entries[str(doc)]["document_id"] == 7
    assert state.entries[str(doc)]["checksum"] == sha256(b"genuinely imported")


def test_other_statuses_are_never_revoked(importer, tmp_path):
    """Only "exists" came from the unsafe path; a submission is still a submission."""
    doc = write_file(tmp_path / "invoice.pdf", b"already submitted")
    state_path = write_legacy_state(
        tmp_path / "state.json",
        {str(doc): legacy_entry(doc, b"already submitted", status="submitted")},
    )

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()

    assert state.entries[str(doc)]["status"] == "submitted"


def test_current_version_file_is_left_alone(importer, tmp_path):
    doc = write_file(tmp_path / "invoice.pdf", b"content")
    digest = sha256(b"content")
    payload = {
        "version": importer.STATE_VERSION,
        "documents": {
            str(doc): {
                "size": 7,
                "mtime": doc.stat().st_mtime,
                "checksum": digest,
                "status": "exists",
                "verified_by": "api-checksum",
            }
        },
    }
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    state = importer.ImportState(str(state_path))
    state.load()
    rehashed, revoked = state.migrate()

    assert (rehashed, revoked) == (0, 0)
    assert state.entries[str(doc)]["checksum"] == digest


def test_unreadable_version_is_still_rejected(importer, tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"version": 99, "documents": {"x": {}}}), encoding="utf-8")

    state = importer.ImportState(str(state_path))
    state.load()

    assert state.entries == {}


def test_migrated_state_is_written_back_at_the_new_version(importer, tmp_path):
    content = b"round trip"
    doc = write_file(tmp_path / "invoice.pdf", content)
    state_path = write_legacy_state(
        tmp_path / "state.json", {str(doc): legacy_entry(doc, content)}
    )

    state = importer.ImportState(str(state_path))
    state.load()
    state.migrate()
    state.save(force=True)

    written = json.loads(state_path.read_text(encoding="utf-8"))
    assert written["version"] == importer.STATE_VERSION
    assert written["checksum_algorithm"] == "sha256"
    assert written["documents"][str(doc)]["checksum"] == sha256(content)
