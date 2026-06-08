"""Tests for the legacy HERMES_* -> THOTH_* .env translator."""

from __future__ import annotations

from pathlib import Path

from thoth_cli.env_translator import translate_env_file_legacy_to_thoth


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return p


def test_simple_assignment(tmp_path):
    p = _write(tmp_path, "HERMES_PG_DSN=postgres://x\n")
    assert translate_env_file_legacy_to_thoth(p) == 1
    assert p.read_text() == "THOTH_PG_DSN=postgres://x\n"


def test_leading_whitespace_preserved(tmp_path):
    p = _write(tmp_path, "  HERMES_X=value\n")
    assert translate_env_file_legacy_to_thoth(p) == 1
    assert p.read_text() == "  THOTH_X=value\n"


def test_export_form(tmp_path):
    p = _write(tmp_path, "export HERMES_TOKEN=abc\n")
    assert translate_env_file_legacy_to_thoth(p) == 1
    assert p.read_text() == "export THOTH_TOKEN=abc\n"


def test_comments_and_blanks_preserved(tmp_path):
    body = "# HERMES_X=old comment\n\nHERMES_Y=keep\n# trailing\n"
    p = _write(tmp_path, body)
    assert translate_env_file_legacy_to_thoth(p) == 1
    assert p.read_text() == "# HERMES_X=old comment\n\nTHOTH_Y=keep\n# trailing\n"


def test_already_thoth_untouched(tmp_path):
    p = _write(tmp_path, "THOTH_X=v\n")
    assert translate_env_file_legacy_to_thoth(p) == 0
    assert p.read_text() == "THOTH_X=v\n"


def test_mixed_file(tmp_path):
    body = "THOTH_A=1\nHERMES_B=2\n# note\nHERMES_C=3\n"
    p = _write(tmp_path, body)
    assert translate_env_file_legacy_to_thoth(p) == 2
    assert p.read_text() == "THOTH_A=1\nTHOTH_B=2\n# note\nTHOTH_C=3\n"


def test_value_with_equals_preserved(tmp_path):
    p = _write(tmp_path, "HERMES_URL=a=b&c=d\n")
    assert translate_env_file_legacy_to_thoth(p) == 1
    assert p.read_text() == "THOTH_URL=a=b&c=d\n"


def test_idempotent(tmp_path):
    p = _write(tmp_path, "HERMES_X=1\nHERMES_Y=2\n")
    assert translate_env_file_legacy_to_thoth(p) == 2
    first = p.read_text()
    assert translate_env_file_legacy_to_thoth(p) == 0
    assert p.read_text() == first


def test_no_trailing_newline_preserved(tmp_path):
    p = _write(tmp_path, "HERMES_X=1")
    assert translate_env_file_legacy_to_thoth(p) == 1
    assert p.read_text() == "THOTH_X=1"


def test_missing_file_returns_zero(tmp_path):
    assert translate_env_file_legacy_to_thoth(tmp_path / "nope.env") == 0
