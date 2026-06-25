"""ffmpeg/ffprobe frame extraction and probing.

SDK-free (subprocess + json only) so it can be exercised in tests wherever ffmpeg
is installed, independent of the MCP SDK. Frame extraction uses a fast approximate
keyframe seek (`-ss` before `-i`) and emits PNG to stdout — no temp files
(design §5.4).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional


def _timeout_msg(binary: str, timeout: float) -> str:
    """A model-facing message for a subprocess timeout that nudges the model to ease
    off — a timeout here usually means too many heavy frame/probe calls at once."""
    return (
        f"{binary} timed out after {timeout:g}s. The server may be overloaded — "
        f"slow down and avoid issuing many video/frame requests at once, then retry. "
        f"(An operator can raise or disable this limit with --ffmpeg-timeout.)"
    )


def ffprobe(p: Path, timeout: Optional[float] = None) -> dict:
    """Return the parsed ``ffprobe -of json`` (format + streams) for a media file.

    `timeout` (seconds, None = unbounded) caps the probe so a pathological file
    cannot hang the server.
    """
    cmd = ["ffprobe", "-loglevel", "error", "-show_format", "-show_streams", "-of", "json", str(p)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(_timeout_msg("ffprobe", timeout))
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300] or "ffprobe failed")
    return json.loads(res.stdout)


def duration(p: Path, timeout: Optional[float] = None) -> float:
    """Best-effort media duration in seconds (format duration, then any stream)."""
    info = ffprobe(p, timeout)
    dur = info.get("format", {}).get("duration")
    if dur is None:
        for s in info.get("streams", []):
            if s.get("duration"):
                dur = s["duration"]
                break
    return float(dur) if dur else 0.0


def norm_format(fmt: str) -> str:
    """Normalize a caller format string to 'png' or 'jpeg'."""
    f = (fmt or "png").lower()
    return "jpeg" if f in ("jpg", "jpeg") else "png"


def _jpeg_qscale(quality: int) -> int:
    """Map a 1..100 quality (higher=better) to ffmpeg mjpeg -q:v (2=best..31=worst)."""
    q = max(1, min(100, int(quality)))
    return max(2, min(31, round(2 + (100 - q) * (31 - 2) / 99)))


def extract_frame(p: Path, t: float, max_dim: Optional[int],
                  fmt: str = "png", quality: int = 85,
                  timeout: Optional[float] = None) -> bytes:
    """Grab one frame at time `t` (seconds). Fast keyframe seek (-ss before -i).

    `fmt` is 'png' (lossless) or 'jpeg' (far smaller; `quality` 1..100 applies).
    `timeout` (seconds, None = unbounded) caps extraction so a pathological file
    cannot hang the server.
    """
    fmt = norm_format(fmt)
    cmd = ["ffmpeg", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(p), "-frames:v", "1"]
    if max_dim:
        cmd += ["-vf", f"scale='min({int(max_dim)},iw)':-2"]
    if fmt == "jpeg":
        cmd += ["-c:v", "mjpeg", "-q:v", str(_jpeg_qscale(quality))]
    else:
        cmd += ["-c:v", "png"]
    cmd += ["-f", "image2", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(_timeout_msg("ffmpeg", timeout))
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError(res.stderr.decode(errors="replace").strip()[:300] or "ffmpeg produced no frame")
    return res.stdout
