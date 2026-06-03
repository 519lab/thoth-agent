"""Tests for thoth_cli.cli_name — resolving the user-facing launcher name.

A Substrate fork installed side-by-side under a different launcher name
(e.g. ``thoth-substrate``) must echo that name back in resume/setup hints,
not the hardcoded default ``thoth`` (which may point at another install).
``THOTH_CLI_NAME`` is the canonical env var; ``HERMES_CLI_NAME`` is honored
as a legacy fallback for back-compat.
"""

import sys

import pytest

from thoth_cli.cli_name import cli_name


class TestCliName:
    def test_env_var_is_authoritative(self, monkeypatch):
        """The launcher shim's THOTH_CLI_NAME wins: the shim execs the venv
        console script (named "thoth"), so argv[0] can't be trusted to carry
        the name the user actually typed."""
        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/thoth", "--resume", "x"])
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        monkeypatch.setenv("THOTH_CLI_NAME", "thoth-substrate")
        assert cli_name() == "thoth-substrate"

    def test_hermes_env_var_fallback(self, monkeypatch):
        """Legacy HERMES_CLI_NAME is still honored as a fallback (back-compat)
        when THOTH_CLI_NAME is unset — older shims export only HERMES_CLI_NAME."""
        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/thoth", "--resume", "x"])
        monkeypatch.delenv("THOTH_CLI_NAME", raising=False)
        monkeypatch.setenv("HERMES_CLI_NAME", "hermes-substrate")
        assert cli_name() == "hermes-substrate"

    def test_thoth_env_wins_when_both_set(self, monkeypatch):
        """When both are exported (current shim), the canonical THOTH_CLI_NAME
        takes precedence over the legacy HERMES_CLI_NAME."""
        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/thoth", "--resume", "x"])
        monkeypatch.setenv("HERMES_CLI_NAME", "hermes-legacy")
        monkeypatch.setenv("THOTH_CLI_NAME", "thoth-substrate")
        assert cli_name() == "thoth-substrate"

    def test_argv0_basename_when_no_env(self, monkeypatch):
        """Without the env var (dev checkout, renamed standalone exe), fall
        back to argv[0]."""
        monkeypatch.setattr(sys, "argv", ["/usr/local/bin/thoth-substrate", "--resume", "x"])
        monkeypatch.delenv("THOTH_CLI_NAME", raising=False)
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        assert cli_name() == "thoth-substrate"

    def test_plain_thoth(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["/home/u/.local/bin/thoth"])
        monkeypatch.delenv("THOTH_CLI_NAME", raising=False)
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        assert cli_name() == "thoth"

    def test_strips_windows_exe_suffix(self, monkeypatch):
        # Bare basename keeps the test platform-independent (os.path.basename
        # only splits on backslash under ntpath, not posixpath).
        monkeypatch.setattr(sys, "argv", ["thoth-substrate.exe"])
        monkeypatch.delenv("THOTH_CLI_NAME", raising=False)
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        assert cli_name() == "thoth-substrate"

    @pytest.mark.parametrize("argv0", [
        "/usr/lib/python3.11/site-packages/thoth_cli/__main__.py",
        "__main__.py",
        "main.py",
        "/usr/bin/python3",
        "python",
        "pytest",
        "-c",
        "",
    ])
    def test_module_and_interpreter_launches_fall_through_to_env(self, monkeypatch, argv0):
        """Module/interpreter launches don't reflect a command name, so the
        launcher-provided env var is consulted."""
        monkeypatch.setattr(sys, "argv", [argv0])
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        monkeypatch.setenv("THOTH_CLI_NAME", "thoth-substrate")
        assert cli_name() == "thoth-substrate"

    def test_default_when_nothing_resolvable(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["__main__.py"])
        monkeypatch.delenv("THOTH_CLI_NAME", raising=False)
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        # sys.executable is a python interpreter → also skipped → default.
        monkeypatch.setattr(sys, "executable", "/usr/bin/python3.11")
        assert cli_name() == "thoth"

    def test_env_basename_only(self, monkeypatch):
        """A path in THOTH_CLI_NAME is reduced to its basename."""
        monkeypatch.setattr(sys, "argv", ["python"])
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        monkeypatch.setenv("THOTH_CLI_NAME", "/opt/bin/thoth-substrate")
        assert cli_name() == "thoth-substrate"

    def test_blank_env_falls_through_to_argv0(self, monkeypatch):
        """An empty/whitespace env var is ignored in favor of argv[0]."""
        monkeypatch.setattr(sys, "argv", ["/usr/local/bin/thoth-fork"])
        monkeypatch.delenv("HERMES_CLI_NAME", raising=False)
        monkeypatch.setenv("THOTH_CLI_NAME", "   ")
        assert cli_name() == "thoth-fork"
