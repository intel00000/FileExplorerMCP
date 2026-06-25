"""ffmpeg/ffprobe timeout handling.

SDK-free and ffmpeg-free: subprocess.run is patched to raise TimeoutExpired, so we
assert the media layer converts it into a RuntimeError whose message nudges the
model to slow down (the #2 guard)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from filebridge_mcp.media import video


def _raise_timeout(*args, **kwargs):
    raise subprocess.TimeoutExpired(cmd="x", timeout=kwargs.get("timeout"))


def test_ffprobe_timeout_becomes_slowdown_runtimeerror(monkeypatch):
    monkeypatch.setattr(video.subprocess, "run", _raise_timeout)
    with pytest.raises(RuntimeError) as ei:
        video.ffprobe(Path("clip.mp4"), timeout=5)
    msg = str(ei.value).lower()
    assert "ffprobe" in msg and "slow down" in msg


def test_extract_frame_timeout_becomes_slowdown_runtimeerror(monkeypatch):
    monkeypatch.setattr(video.subprocess, "run", _raise_timeout)
    with pytest.raises(RuntimeError) as ei:
        video.extract_frame(Path("clip.mp4"), 1.0, None, "png", 85, timeout=5)
    msg = str(ei.value).lower()
    assert "ffmpeg" in msg and "slow down" in msg


def test_no_timeout_passes_through(monkeypatch):
    """When unbounded (timeout=None) the call still works — subprocess gets timeout=None."""
    seen = {}

    class _Res:
        returncode = 0
        stdout = b"\x89PNG\r\n\x1a\n"
        stderr = b""

    def _fake_run(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout", "MISSING")
        return _Res()

    monkeypatch.setattr(video.subprocess, "run", _fake_run)
    out = video.extract_frame(Path("clip.mp4"), 1.0, None, "png", 85, timeout=None)
    assert out.startswith(b"\x89PNG")
    assert seen["timeout"] is None
