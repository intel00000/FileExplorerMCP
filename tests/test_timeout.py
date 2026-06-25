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


def test_size_scaled_timeout(tmp_path):
    """Scene-detection budget scales ~30s/GB with a one-GB-worth floor for small files."""
    import os

    from filebridge_mcp.media.video import size_scaled_timeout

    tiny = tmp_path / "tiny.bin"
    tiny.write_bytes(b"x")
    assert size_scaled_timeout(tiny) == 30.0  # floored at one GB's worth

    one_gb = tmp_path / "one.bin"
    one_gb.write_bytes(b"")
    os.truncate(one_gb, 1_000_000_000)  # sparse 1 GB
    assert size_scaled_timeout(one_gb) == 30.0

    two_gb = tmp_path / "two.bin"
    two_gb.write_bytes(b"")
    os.truncate(two_gb, 2_000_000_000)  # sparse 2 GB
    assert size_scaled_timeout(two_gb) == 60.0

    # Missing file falls back to the floor rather than raising.
    assert size_scaled_timeout(tmp_path / "nope.bin") == 30.0
