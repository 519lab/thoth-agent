"""Fixtures shared across thoth_cli kanban tests."""

from __future__ import annotations

import pytest


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Thoth profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from thoth_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``thoth`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from thoth_cli import main as _cli_main
    except Exception:
        return
    # hermes_bootstrap (imported via thoth_cli.main) calls normalize_thoth_home_env()
    # at module-load time, which sets THOTH_HOME = HERMES_HOME. On the first import
    # inside a test this runs AFTER the root conftest cleared THOTH_HOME, undoing the
    # clear. Re-clear so tests that only patch HERMES_HOME are not shadowed by THOTH_HOME.
    monkeypatch.delenv("THOTH_HOME", raising=False)
    monkeypatch.setattr(
        _cli_main, "_detect_concurrent_hermes_instances", lambda *_a, **_k: []
    )
