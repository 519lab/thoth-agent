"""Tests for migrate_home_to_thoth() — P3c of the Hermes→Thoth rename."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from thoth_cli.config import migrate_home_to_thoth


class TestMigrateHomeToThoth:
    def test_creates_symlink_when_thoth_exists_thoth_absent(self, tmp_path):
        """~/.hermes exists, ~/.thoth absent → symlink created."""
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        thoth = tmp_path / ".thoth"

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            result = migrate_home_to_thoth(quiet=True)

        assert result is True
        assert thoth.is_symlink()
        assert thoth.resolve() == hermes.resolve()

    def test_noop_when_thoth_already_exists_as_dir(self, tmp_path):
        """~/.thoth is already a real directory → no-op."""
        (tmp_path / ".hermes").mkdir()
        (tmp_path / ".thoth").mkdir()

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            result = migrate_home_to_thoth(quiet=True)

        assert result is False
        assert (tmp_path / ".thoth").is_dir()
        assert not (tmp_path / ".thoth").is_symlink()

    def test_noop_when_thoth_already_exists_as_symlink(self, tmp_path):
        """~/.thoth is already a symlink → no-op (already migrated)."""
        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        thoth = tmp_path / ".thoth"
        thoth.symlink_to(hermes)

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            result = migrate_home_to_thoth(quiet=True)

        assert result is False

    def test_noop_when_thoth_absent(self, tmp_path):
        """Neither directory exists (fresh install) → no-op; ensure_thoth_home handles it."""
        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            result = migrate_home_to_thoth(quiet=True)

        assert result is False
        assert not (tmp_path / ".thoth").exists()

    def test_noop_when_thoth_is_file_not_dir(self, tmp_path):
        """~/.hermes exists but is a file, not a directory → no-op."""
        (tmp_path / ".hermes").write_text("oops")

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            result = migrate_home_to_thoth(quiet=True)

        assert result is False

    def test_verbose_output_on_success(self, tmp_path, capsys):
        """quiet=False prints a success line."""
        (tmp_path / ".hermes").mkdir()

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            result = migrate_home_to_thoth(quiet=False)

        assert result is True
        out = capsys.readouterr().out
        assert "~/.thoth" in out
        assert "~/.hermes" in out

    def test_symlink_target_is_absolute(self, tmp_path):
        """Symlink target must be absolute so it resolves from any cwd."""
        (tmp_path / ".hermes").mkdir()

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            migrate_home_to_thoth(quiet=True)

        link = tmp_path / ".thoth"
        assert link.is_symlink()
        # symlink_to(thoth_dir) where thoth_dir is absolute → target is absolute
        assert Path(link.readlink()).is_absolute()

    def test_ensure_thoth_home_triggers_migration_silently(self, tmp_path, monkeypatch):
        """ensure_thoth_home() auto-creates the symlink without printing."""
        from thoth_cli.config import ensure_thoth_home

        hermes = tmp_path / ".hermes"
        hermes.mkdir()
        monkeypatch.setenv("THOTH_HOME", str(hermes))
        monkeypatch.setenv("THOTH_HOME", str(hermes))

        with patch("thoth_cli.config.Path.home", return_value=tmp_path):
            import io, sys
            buf = io.StringIO()
            old = sys.stdout
            sys.stdout = buf
            try:
                ensure_thoth_home()
            finally:
                sys.stdout = old
            captured = buf.getvalue()

        thoth = tmp_path / ".thoth"
        assert thoth.is_symlink()
        # Silent: no output about migration
        assert "symlink" not in captured.lower()
        assert "thoth" not in captured.lower()

    def test_noop_on_windows(self, tmp_path):
        """Windows is skipped regardless of filesystem state (symlinks need elevation)."""
        (tmp_path / ".hermes").mkdir()

        with patch("thoth_cli.config.Path.home", return_value=tmp_path), \
             patch("thoth_cli.config.sys.platform", "win32"):
            result = migrate_home_to_thoth(quiet=True)

        assert result is False
        assert not (tmp_path / ".thoth").exists()
