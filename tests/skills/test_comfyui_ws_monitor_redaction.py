"""ws_monitor must not log the Comfy Cloud API key it puts in the websocket URL.

Skill-script stderr lands in tool output and session transcripts, so the
"Connecting to ..." line has to carry a redacted URL even though the real
connection URL keeps ``&token=<key>``.
"""
import runpy
import sys
import types
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "creative" / "comfyui" / "scripts"
CANARY = "sk-test-canary-0123456789abcdef"


@pytest.fixture
def stub_websocket(monkeypatch):
    seen_urls = []
    fake = types.ModuleType("websocket")

    def create_connection(url, timeout=None):
        seen_urls.append(url)
        raise ConnectionRefusedError("stubbed: no network in tests")

    fake.create_connection = create_connection
    fake.WebSocketTimeoutException = TimeoutError
    fake.WebSocketConnectionClosedException = ConnectionError
    monkeypatch.setitem(sys.modules, "websocket", fake)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return seen_urls


def _run_monitor(monkeypatch, host):
    script = SCRIPTS / "ws_monitor.py"
    monkeypatch.setattr(sys, "argv", [
        str(script), "--host", host, "--api-key", CANARY, "--no-color", "--timeout", "1",
    ])
    with pytest.raises((ConnectionRefusedError, SystemExit)):
        runpy.run_path(str(script), run_name="__main__")


def test_cloud_token_is_redacted_in_log_but_sent_on_connect(monkeypatch, capsys, stub_websocket):
    _run_monitor(monkeypatch, "https://cloud.comfy.org")

    err = capsys.readouterr().err
    assert "Connecting to" in err
    assert CANARY not in err
    assert "token=<redacted>" in err
    # The real connection still authenticates with the key.
    assert stub_websocket and f"token={CANARY}" in stub_websocket[0]


def test_local_host_gets_no_token(monkeypatch, capsys, stub_websocket):
    _run_monitor(monkeypatch, "http://127.0.0.1:8188")

    err = capsys.readouterr().err
    assert CANARY not in err
    assert "token=" not in err
    assert stub_websocket and "token=" not in stub_websocket[0]
