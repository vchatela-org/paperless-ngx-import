"""The digest has to be the one Paperless stores, or dedup matches nothing."""

import hashlib

from conftest import write_file


def test_checksum_is_sha256(importer, tmp_path):
    content = b"a document, of sorts"
    path = write_file(tmp_path / "doc.pdf", content)

    digest = importer.calculate_file_checksum(str(path))

    assert digest == hashlib.sha256(content).hexdigest()


def test_checksum_is_64_hex_characters(importer, tmp_path):
    """Paperless stores a 64-char digest; a 32-char one can never match it."""
    path = write_file(tmp_path / "doc.pdf", b"content")

    digest = importer.calculate_file_checksum(str(path))

    assert len(digest) == importer.CHECKSUM_HEX_LENGTH
    assert len(digest) != importer.LEGACY_CHECKSUM_HEX_LENGTH


def test_checksum_reads_files_larger_than_one_chunk(importer, tmp_path):
    content = b"x" * (3 * 1024 * 1024 + 17)
    path = write_file(tmp_path / "big.pdf", content)

    assert importer.calculate_file_checksum(str(path)) == hashlib.sha256(content).hexdigest()


def test_unreadable_file_yields_no_checksum(importer, tmp_path):
    assert importer.calculate_file_checksum(str(tmp_path / "missing.pdf")) is None
