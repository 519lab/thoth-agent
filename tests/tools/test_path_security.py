"""Tests for ``tools/path_security.py`` — the shared path-traversal guard.

``validate_within_dir`` and ``has_traversal_component`` are the sandbox
boundary for every tool that touches a user-supplied path
(skill_manager_tool, skills_tool, cronjob_tools, credential_files, …). A
regression here is a directory-escape / arbitrary-file-access bug, so the
escape cases below are the load-bearing ones — not the happy path.
"""

import os
from pathlib import Path

import pytest

from tools.path_security import has_traversal_component, validate_within_dir


class TestValidateWithinDir:
    def test_path_inside_root_is_allowed(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        target = root / "sub" / "file.txt"
        assert validate_within_dir(target, root) is None

    def test_root_itself_is_within_root(self, tmp_path):
        # relative_to(self) is the empty path, not an error.
        assert validate_within_dir(tmp_path, tmp_path) is None

    def test_nonexistent_path_inside_root_is_allowed(self, tmp_path):
        # resolve() does not require the path to exist; a not-yet-created
        # file under the root must still validate (callers create it after).
        target = tmp_path / "does" / "not" / "exist.txt"
        assert validate_within_dir(target, tmp_path) is None

    def test_sibling_directory_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        sibling = tmp_path / "evil"
        root.mkdir()
        sibling.mkdir()
        err = validate_within_dir(sibling / "secret.txt", root)
        assert err is not None
        assert "escapes allowed directory" in err

    def test_dotdot_escape_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        # root/sub/../../escape resolves to tmp_path/escape — outside root.
        escaping = root / "sub" / ".." / ".." / "escape.txt"
        err = validate_within_dir(escaping, root)
        assert err is not None
        assert "escapes allowed directory" in err

    def test_dotdot_that_stays_inside_is_allowed(self, tmp_path):
        root = tmp_path / "root"
        (root / "a").mkdir(parents=True)
        # root/a/../b resolves back to root/b — still inside.
        inside = root / "a" / ".." / "b.txt"
        assert validate_within_dir(inside, root) is None

    def test_symlink_escaping_root_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        outside = tmp_path / "outside"
        root.mkdir()
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("classified", encoding="utf-8")

        link = root / "link"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")

        # resolve() follows the symlink out of root -> must be rejected.
        err = validate_within_dir(link, root)
        assert err is not None
        assert "escapes allowed directory" in err

    def test_symlink_staying_inside_root_is_allowed(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        real = root / "real.txt"
        real.write_text("ok", encoding="utf-8")
        link = root / "link"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform/filesystem")
        assert validate_within_dir(link, root) is None

    def test_prefix_sibling_is_not_treated_as_inside(self, tmp_path):
        # "/x/root" must NOT contain "/x/root-evil" even though the string
        # "root" is a prefix of "root-evil". relative_to is path-component
        # aware, so this is correctly rejected.
        root = tmp_path / "root"
        evil = tmp_path / "root-evil"
        root.mkdir()
        evil.mkdir()
        err = validate_within_dir(evil / "f.txt", root)
        assert err is not None


class TestHasTraversalComponent:
    @pytest.mark.parametrize(
        "path_str",
        [
            "../etc/passwd",
            "a/../b",
            "a/b/..",
            "../../x",
            "sub/../../escape",
        ],
    )
    def test_detects_traversal(self, path_str):
        assert has_traversal_component(path_str) is True

    @pytest.mark.parametrize(
        "path_str",
        [
            "a/b/c",
            "file.txt",
            "/abs/path/file",
            "..foo",        # a filename that merely starts with dots
            "foo..",        # trailing dots, not a component
            "a/...b/c",     # three dots, not a traversal component
            "",             # empty string -> no parts
        ],
    )
    def test_ignores_non_traversal(self, path_str):
        assert has_traversal_component(path_str) is False

    def test_quick_check_does_not_resolve_filesystem(self):
        # has_traversal_component is a pure string check — it must report a
        # ".." regardless of whether the path exists on disk.
        assert has_traversal_component("definitely/not/real/../path") is True
