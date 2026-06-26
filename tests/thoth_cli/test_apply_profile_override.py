"""Regression tests for _apply_profile_override THOTH_HOME guard (issue #22502).

When THOTH_HOME is set to the thoth root (e.g. systemd hardcodes
THOTH_HOME=/root/.thoth), _apply_profile_override must still read
active_profile and update THOTH_HOME to the profile directory.

When THOTH_HOME is already a profile directory (.../profiles/<name>),
_apply_profile_override must trust it and return without re-reading
active_profile (child-process inheritance contract).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _run_apply_profile_override(
    tmp_path, monkeypatch, *, thoth_home: str | None, active_profile: str | None,
    argv: list[str] | None = None,
):
    """Run _apply_profile_override in isolation.

    Returns the value of os.environ["THOTH_HOME"] after the call,
    or None if unset.
    """
    thoth_root = tmp_path / ".thoth"
    thoth_root.mkdir(parents=True, exist_ok=True)

    if active_profile is not None:
        (thoth_root / "active_profile").write_text(active_profile)

    if active_profile and active_profile != "default":
        (thoth_root / "profiles" / active_profile).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("THOTH_HOME", raising=False)
    if thoth_home is not None:
        monkeypatch.setenv("THOTH_HOME", thoth_home)
    else:
        monkeypatch.delenv("THOTH_HOME", raising=False)

    monkeypatch.setattr(sys, "argv", argv or ["thoth", "gateway", "start"])

    from thoth_cli.main import _apply_profile_override
    _apply_profile_override()

    return os.environ.get("THOTH_HOME")


class TestApplyProfileOverrideHermesHomeGuard:
    """Regression guard for issue #22502.

    Verifies that THOTH_HOME pointing to the thoth root does NOT suppress
    the active_profile check, while THOTH_HOME already pointing to a
    profile directory IS trusted as-is.
    """

    def test_thoth_home_at_root_with_active_profile_is_redirected(
        self, tmp_path, monkeypatch
    ):
        """THOTH_HOME=/root/.thoth + active_profile=coder must redirect
        THOTH_HOME to .../profiles/coder.

        Bug scenario from #22502: systemd sets THOTH_HOME to the thoth root
        and the user switches to a profile via `thoth profile use`.
        Before the fix, the guard returned early and active_profile was ignored.
        """
        thoth_root = tmp_path / ".thoth"
        thoth_root.mkdir(parents=True, exist_ok=True)

        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            thoth_home=str(thoth_root),
            active_profile="coder",
        )

        assert result is not None, "THOTH_HOME must be set after profile redirect"
        assert "profiles" in result, (
            f"Expected THOTH_HOME to point into profiles/ dir, got: {result!r}"
        )
        assert result.endswith("coder"), (
            f"Expected THOTH_HOME to end with 'coder', got: {result!r}"
        )

    def test_thoth_home_already_profile_dir_is_trusted(self, tmp_path, monkeypatch):
        """THOTH_HOME=.../profiles/coder must not be overridden even when
        active_profile says something different.

        Preserves the child-process inheritance contract: a subprocess spawned
        with THOTH_HOME already set to a specific profile must stay in that
        profile.
        """
        thoth_root = tmp_path / ".thoth"
        profile_dir = thoth_root / "profiles" / "coder"
        profile_dir.mkdir(parents=True, exist_ok=True)

        (thoth_root / "active_profile").write_text("other")

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("THOTH_HOME", str(profile_dir))
        monkeypatch.setattr(sys, "argv", ["thoth", "gateway", "start"])

        from thoth_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("THOTH_HOME") == str(profile_dir), (
            "THOTH_HOME must remain unchanged when already pointing to a profile dir"
        )

    def test_thoth_home_unset_reads_active_profile(self, tmp_path, monkeypatch):
        """Classic case: THOTH_HOME unset + active_profile=coder must set
        THOTH_HOME to the profile directory (existing behaviour must not regress).
        """
        result = _run_apply_profile_override(
            tmp_path,
            monkeypatch,
            thoth_home=None,
            active_profile="coder",
        )

        assert result is not None
        assert "coder" in result

    def test_thoth_home_unset_default_profile_no_redirect(self, tmp_path, monkeypatch):
        """active_profile=default must not redirect THOTH_HOME."""
        thoth_root = tmp_path / ".thoth"
        thoth_root.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.delenv("THOTH_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["thoth", "gateway", "start"])
        (thoth_root / "active_profile").write_text("default")

        from thoth_cli.main import _apply_profile_override
        _apply_profile_override()

        assert os.environ.get("THOTH_HOME") is None
