"""Tests for the workspace permission-boundary write/patch guard.

Pure logic — no DB.  Drives ``maybe_require_workspace_boundary_approval`` across
the write_file / patch / V4A-delete cases, verifying it prompts (via a stubbed
approval callback) for targets outside the active root, allows targets inside
the root without prompting, never inspects reads, and fails closed on its own
internal error.
"""

import json
import tempfile

import pytest

import agent.file_safety as file_safety
import tools.approval as approval_module
import tools.terminal_tool as terminal_tool
from agent.workspace_boundary import maybe_require_workspace_boundary_approval


class _StubCallback:
    """Records approval prompts and returns a preset choice."""

    def __init__(self, choice="deny"):
        self.choice = choice
        self.calls = []

    def __call__(self, command, description, *, allow_permanent=True):
        self.calls.append((command, description, allow_permanent))
        return self.choice


@pytest.fixture
def boundary_env(tmp_path, monkeypatch):
    """Active root + an outside dir; interactive CLI; deterministic approvals."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    monkeypatch.setenv("THOTH_ACTIVE_ROOT", str(root))
    monkeypatch.setenv("THOTH_INTERACTIVE", "1")
    monkeypatch.delenv("THOTH_YOLO_MODE", raising=False)
    monkeypatch.delenv("THOTH_NO_WORKSPACE_BOUNDARY", raising=False)
    monkeypatch.delenv("THOTH_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("THOTH_EXEC_ASK", raising=False)
    monkeypatch.delenv("THOTH_CRON_SESSION", raising=False)

    # The system temp dir normally counts as "inside" (scratch space); point it
    # at an isolated path so our tmp_path-based "outside" dir is genuinely out.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "_isolated_tmp"))

    # Deterministic: boundary on, manual approval mode (no aux-LLM, no yolo).
    monkeypatch.setattr(file_safety, "boundary_enabled", lambda: True)
    monkeypatch.setattr(approval_module, "_get_approval_mode", lambda: "manual")

    # Reset module-level approval caches between tests.
    approval_module._session_approved.clear()
    approval_module._permanent_approved.clear()
    approval_module._gateway_queues.clear()
    approval_module._gateway_notify_cbs.clear()

    yield root, outside

    terminal_tool.set_approval_callback(None)
    approval_module._session_approved.clear()
    approval_module._permanent_approved.clear()


def test_write_file_outside_root_prompts_and_denies(boundary_env):
    root, outside = boundary_env
    cb = _StubCallback(choice="deny")
    terminal_tool.set_approval_callback(cb)

    target = str(outside / "note.txt")
    result = maybe_require_workspace_boundary_approval(
        "write_file", {"path": target}, "default"
    )

    assert result is not None
    payload = json.loads(result)
    assert "Write denied" in payload["error"]
    assert target in payload["error"]
    assert len(cb.calls) == 1


def test_write_file_outside_root_prompts_and_allows_once(boundary_env):
    root, outside = boundary_env
    cb = _StubCallback(choice="once")
    terminal_tool.set_approval_callback(cb)

    target = str(outside / "note.txt")
    result = maybe_require_workspace_boundary_approval(
        "write_file", {"path": target}, "default"
    )

    assert result is None
    assert len(cb.calls) == 1


def test_patch_outside_root_prompts(boundary_env):
    root, outside = boundary_env
    cb = _StubCallback(choice="deny")
    terminal_tool.set_approval_callback(cb)

    target = str(outside / "code.py")
    result = maybe_require_workspace_boundary_approval(
        "patch", {"path": target, "old_string": "a", "new_string": "b"}, "default"
    )

    assert result is not None
    assert "Write denied" in json.loads(result)["error"]
    assert len(cb.calls) == 1


def test_v4a_delete_outside_root_prompts(boundary_env):
    root, outside = boundary_env
    cb = _StubCallback(choice="deny")
    terminal_tool.set_approval_callback(cb)

    target = str(outside / "gone.py")
    patch_text = f"*** Begin Patch\n*** Delete File: {target}\n*** End Patch"
    result = maybe_require_workspace_boundary_approval(
        "patch", {"path": None, "patch": patch_text}, "default"
    )

    assert result is not None
    assert target in json.loads(result)["error"]
    assert len(cb.calls) == 1


def test_write_inside_root_returns_none_no_prompt(boundary_env):
    root, outside = boundary_env
    cb = _StubCallback(choice="deny")
    terminal_tool.set_approval_callback(cb)

    target = str(root / "subdir" / "file.txt")
    result = maybe_require_workspace_boundary_approval(
        "write_file", {"path": target}, "default"
    )

    assert result is None
    assert cb.calls == []


def test_read_file_never_inspected(boundary_env):
    root, outside = boundary_env
    cb = _StubCallback(choice="deny")
    terminal_tool.set_approval_callback(cb)

    # Even a path far outside the root must not be inspected for reads.
    result = maybe_require_workspace_boundary_approval(
        "read_file", {"path": str(outside / "secret.txt")}, "default"
    )

    assert result is None
    assert cb.calls == []


def test_internal_error_fails_closed(boundary_env, monkeypatch):
    root, outside = boundary_env
    terminal_tool.set_approval_callback(_StubCallback(choice="once"))

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(file_safety, "check_path_boundary", _boom)

    result = maybe_require_workspace_boundary_approval(
        "write_file", {"path": str(outside / "x.txt")}, "default"
    )

    assert result is not None
    assert json.loads(result)["error"] == "Write denied: workspace boundary guard failed"
