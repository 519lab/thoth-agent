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
