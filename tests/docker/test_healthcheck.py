"""Tests for the compose gateway pid-file healthcheck."""

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "healthcheck.py"


@pytest.fixture
def healthcheck():
    spec = importlib.util.spec_from_file_location("thoth_docker_healthcheck", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_pid(home: Path, payload) -> None:
    (home / "gateway.pid").write_text(json.dumps(payload), encoding="utf-8")


def test_live_pid_is_healthy(healthcheck, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    _write_pid(tmp_path, {"pid": os.getpid()})
    assert healthcheck.main() == 0
    assert f"healthy: gateway pid {os.getpid()} alive" in capsys.readouterr().out


def test_missing_pid_file_is_unhealthy(healthcheck, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    assert healthcheck.main() == 1
    assert "cannot read gateway pid" in capsys.readouterr().out


def test_malformed_pid_file_is_unhealthy(healthcheck, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    (tmp_path / "gateway.pid").write_text("{not json", encoding="utf-8")
    assert healthcheck.main() == 1
    assert "cannot read gateway pid" in capsys.readouterr().out


def test_pid_file_without_pid_field_is_unhealthy(healthcheck, tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    _write_pid(tmp_path, {"kind": "thoth-gateway"})
    assert healthcheck.main() == 1


def test_stale_pid_is_unhealthy(healthcheck, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    _write_pid(tmp_path, {"pid": 424242})

    def _dead(pid, sig):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(healthcheck.os, "kill", _dead)
    assert healthcheck.main() == 1
    assert "stale pid file" in capsys.readouterr().out


def test_permission_error_counts_as_alive(healthcheck, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    _write_pid(tmp_path, {"pid": 1})

    def _denied(pid, sig):
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(healthcheck.os, "kill", _denied)
    assert healthcheck.main() == 0
    assert "healthy: gateway pid 1 alive" in capsys.readouterr().out


def test_matching_start_time_is_healthy(healthcheck, tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    pid = os.getpid()
    _write_pid(tmp_path, {"pid": pid, "start_time": healthcheck._process_start_time(pid)})
    assert healthcheck.main() == 0


def test_reused_pid_is_unhealthy(healthcheck, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    _write_pid(tmp_path, {"pid": os.getpid(), "start_time": 1})
    monkeypatch.setattr(healthcheck, "_process_start_time", lambda pid: 999)
    assert healthcheck.main() == 1
    assert "reused by another process" in capsys.readouterr().out


def test_unreadable_start_time_falls_back_to_alive(healthcheck, tmp_path, monkeypatch):
    """No /proc (or unreadable stat): the signal-0 result stands."""
    monkeypatch.setenv("THOTH_HOME", str(tmp_path))
    _write_pid(tmp_path, {"pid": os.getpid(), "start_time": 1})
    monkeypatch.setattr(healthcheck, "_process_start_time", lambda pid: None)
    assert healthcheck.main() == 0


def test_process_start_time_matches_gateway_status(healthcheck):
    """The probe must read start_time exactly as gateway/status.py records it."""
    from gateway.status import get_process_start_time

    assert healthcheck._process_start_time(os.getpid()) == get_process_start_time(os.getpid())
