"""Tests for utils.atomic_text_write — crash-safe text writes for secret files."""

import os
import stat
import sys
from unittest.mock import patch

import pytest

from utils import atomic_text_write

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")


def test_writes_utf8_text_and_creates_parents(tmp_path):
    target = tmp_path / "a" / "b" / ".env"
    atomic_text_write(target, "KEY=välue\n")
    assert target.read_text(encoding="utf-8") == "KEY=välue\n"


@posix_only
def test_new_file_is_created_owner_only_even_with_open_umask(tmp_path):
    target = tmp_path / ".env"
    old = os.umask(0o000)
    try:
        atomic_text_write(target, "SECRET=x\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@posix_only
def test_existing_file_keeps_its_mode(tmp_path):
    target = tmp_path / "shared.txt"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o640)
    atomic_text_write(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_failed_write_leaves_original_and_no_temp_files(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    target = workdir / "keep.txt"
    target.write_text("original", encoding="utf-8")
    with patch("utils.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            atomic_text_write(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"
    assert sorted(p.name for p in workdir.iterdir()) == ["keep.txt"]
