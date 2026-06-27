"""Tests for the agent's active-root / default-workspace resolution (Feature A).

Pure logic — no DB. Drives `resolve_active_root` across the launch scenarios and
verifies `ensure_workspace_dir` creates `{THOTH_HOME}/workspace`.
"""

import os

import pytest

from agent.file_safety import resolve_active_root


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point $HOME and THOTH_HOME at a temp tree; clear any profile override."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("THOTH_HOME", str(h / ".thoth"))
    # Ensure no context override leaks in from a prior import.
    try:
        from thoth_constants import set_thoth_home_override
        set_thoth_home_override(None)
    except Exception:
        pass
    return h


def _real(p):
    return os.path.realpath(os.path.expanduser(str(p)))


def test_explicit_cwd_wins(home, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    got = resolve_active_root(explicit_cwd=str(proj), launch_cwd=str(home), is_gateway=False)
    assert _real(got) == _real(proj)


@pytest.mark.parametrize("placeholder", ["", ".", "auto", "cwd"])
def test_placeholder_cwd_is_not_explicit(home, placeholder):
    # Placeholder + launched from $HOME → workspace default.
    got = resolve_active_root(explicit_cwd=placeholder, launch_cwd=str(home), is_gateway=False)
    assert _real(got) == _real(home / ".thoth" / "workspace")


def test_cli_launched_from_home_uses_workspace(home):
    got = resolve_active_root(explicit_cwd=None, launch_cwd=str(home), is_gateway=False)
    assert _real(got) == _real(home / ".thoth" / "workspace")


def test_cli_launched_inside_thoth_home_uses_workspace(home):
    inside = home / ".thoth" / "skills"
    inside.mkdir(parents=True)
    got = resolve_active_root(explicit_cwd=None, launch_cwd=str(inside), is_gateway=False)
    assert _real(got) == _real(home / ".thoth" / "workspace")


def test_cli_launched_in_project_uses_that_dir(home, tmp_path):
    proj = tmp_path / "myproject"
    proj.mkdir()
    got = resolve_active_root(explicit_cwd=None, launch_cwd=str(proj), is_gateway=False)
    assert _real(got) == _real(proj)


def test_gateway_no_cwd_uses_workspace(home):
    got = resolve_active_root(explicit_cwd=None, launch_cwd=None, is_gateway=True)
    assert _real(got) == _real(home / ".thoth" / "workspace")


def test_gateway_explicit_cwd_wins(home, tmp_path):
    proj = tmp_path / "gwroot"
    proj.mkdir()
    got = resolve_active_root(explicit_cwd=str(proj), launch_cwd=None, is_gateway=True)
    assert _real(got) == _real(proj)


def test_ensure_workspace_dir_creates(home):
    from thoth_constants import ensure_workspace_dir, get_workspace_dir

    ws = get_workspace_dir()
    assert not ws.exists()
    created = ensure_workspace_dir()
    assert created.is_dir()
    assert _real(created) == _real(home / ".thoth" / "workspace")
