"""Tag names come from folder names, so folder punctuation must not survive.

Both tokenizers are pinned here: "legacy" is what the live tag namespace was
built by and must not drift, "clean" is what a migrated deployment gets.
"""

import pytest


@pytest.fixture
def legacy(importer, monkeypatch):
    monkeypatch.setattr(importer, "TAG_TOKENIZER", "legacy")
    return importer


@pytest.fixture
def clean(importer, monkeypatch):
    monkeypatch.setattr(importer, "TAG_TOKENIZER", "clean")
    return importer


# --- what the corpus was tagged with, and must keep being tagged with ---

def test_legacy_splits_punctuation_into_tags(legacy):
    names = legacy.tag_names_from_path("/mnt/documents/Pharmatop (7 Cantons)/bill.pdf")

    assert "(7" in names
    assert "cantons)" in names


def test_legacy_tags_every_file_with_the_watch_dir_component(legacy):
    """IGNORED_PATHS strips "/mnt", leaving "documents" on all 586 documents."""
    assert "documents" in legacy.tag_names_from_path("/mnt/documents/Amazon/bill.pdf")


# --- what the corrected tokenizer produces ---

def test_clean_drops_punctuation_from_tokens(clean):
    names = clean.tag_names_from_path("/mnt/documents/Pharmatop (7 Cantons)/bill.pdf")

    assert names == ["pharmatop", "cantons"]


def test_clean_strips_the_whole_watch_dir(clean):
    assert "documents" not in clean.tag_names_from_path("/mnt/documents/Amazon/bill.pdf")


def test_clean_keeps_meaningful_multi_character_tokens(clean):
    names = clean.tag_names_from_path("/mnt/documents/Amazon/HRM 200 Joel/invoice.pdf")

    assert names == ["amazon", "hrm", "200", "joel"]


@pytest.mark.parametrize("folder", ["-", "—", "...", "p", "A"])
def test_clean_emits_no_tag_for_debris(clean, folder):
    assert clean.tag_names_from_path(f"/mnt/documents/{folder}/bill.pdf") == []


def test_clean_preserves_internal_punctuation(clean):
    """Only the ends are trimmed: "e-mail" is a word, "(7" is not."""
    assert clean.tag_names_from_path("/mnt/documents/e-mail/bill.pdf") == ["e-mail"]


def test_clean_still_honours_ignored_folders(clean):
    names = clean.tag_names_from_path("/mnt/documents/@eaDir/Amazon/bill.pdf")

    assert names == ["amazon"]


def test_clean_falls_back_to_ignored_paths_outside_the_watch_dir(clean):
    """Nothing should be walked from outside WATCH_DIR, but if it is, it still works."""
    names = clean.tag_names_from_path("/mnt/elsewhere/Amazon/bill.pdf")

    assert "amazon" in names


def test_tokenizers_agree_on_names_that_were_never_broken(legacy, clean, monkeypatch):
    path = "/mnt/documents/Amazon/bill.pdf"

    monkeypatch.setattr(legacy, "TAG_TOKENIZER", "legacy")
    legacy_names = legacy.tag_names_from_path(path)
    monkeypatch.setattr(clean, "TAG_TOKENIZER", "clean")
    clean_names = clean.tag_names_from_path(path)

    assert set(legacy_names) - set(clean_names) == {"documents"}


def test_tag_names_are_capped_at_the_paperless_limit(clean):
    long_name = "a" * 200

    assert clean.tag_names_from_path(f"/mnt/documents/{long_name}/bill.pdf") == []


# --- reporting what staying on legacy costs ---

def test_junk_names_are_identified_for_the_operator(legacy):
    junk = legacy.junk_tag_names({"(7", "cantons)", "-", "documents", "amazon", "200"})

    assert junk == ["(7", "-", "cantons)", "documents"]


def test_the_legacy_warning_names_the_offending_tags(legacy, monkeypatch):
    messages = []
    monkeypatch.setattr(
        legacy, "log_message", lambda message, level="INFO": messages.append((level, message))
    )

    legacy.report_legacy_tag_tokens({"(7", "cantons)", "amazon"})

    assert any(level == "WARNING" and "'(7'" in message for level, message in messages)


def test_no_warning_once_the_tokenizer_is_clean(clean, monkeypatch):
    messages = []
    monkeypatch.setattr(
        clean, "log_message", lambda message, level="INFO": messages.append(message)
    )

    clean.report_legacy_tag_tokens({"(7", "cantons)"})

    assert messages == []


def test_an_unknown_tokenizer_is_rejected_at_startup(importer, monkeypatch):
    monkeypatch.setenv("TAG_TOKENIZER", "whatever")

    with pytest.raises(ValueError, match="TAG_TOKENIZER"):
        importer.get_container_config()
