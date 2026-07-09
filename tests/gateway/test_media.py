"""Unit tests for gateway/media.py.

Imports from the extracted module's canonical home (``gateway.media``) rather
than the ``gateway.run`` re-export. ``_probe_audio_duration`` is also patched at
``gateway.run._probe_audio_duration`` by tests/gateway/test_stt_config.py, which
exercises the re-export surface.
"""

import types
import wave

import pytest

from gateway.media import (
    _build_media_placeholder,
    _format_duration,
    _probe_audio_duration,
)


def test_format_duration():
    assert _format_duration(75) == "1:15"
    assert _format_duration(3725) == "1:02:05"
    assert _format_duration(0) == "0:00"
    # Negative durations clamp to zero rather than producing garbage.
    assert _format_duration(-10) == "0:00"
    # Rounds to the nearest whole second.
    assert _format_duration(59.6) == "1:00"


def _event(**kw):
    kw.setdefault("message_type", None)
    return types.SimpleNamespace(**kw)


def test_build_media_placeholder_by_mime_type():
    ev = _event(
        media_urls=["u1", "u2", "u3"],
        media_types=["image/png", "audio/ogg", "application/pdf"],
    )
    out = _build_media_placeholder(ev)
    assert out == (
        "[User sent an image: u1]\n"
        "[User sent audio: u2]\n"
        "[User sent a file: u3]"
    )


def test_build_media_placeholder_empty_when_no_media():
    assert _build_media_placeholder(_event(media_urls=[], media_types=[])) == ""
    # Missing attributes are tolerated (getattr fallbacks).
    assert _build_media_placeholder(types.SimpleNamespace()) == ""


@pytest.mark.asyncio
async def test_probe_audio_duration_wav(tmp_path):
    # A 1-second mono 8kHz wav -> "0:01".
    wav_path = tmp_path / "clip.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000)
    assert await _probe_audio_duration(str(wav_path)) == "0:01"


@pytest.mark.asyncio
async def test_probe_audio_duration_missing_returns_none(tmp_path):
    assert await _probe_audio_duration(str(tmp_path / "nope.wav")) is None
