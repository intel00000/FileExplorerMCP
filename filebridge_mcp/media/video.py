"""ffmpeg/ffprobe frame extraction and probing.

SDK-free (subprocess + json only) so it can be exercised in tests wherever ffmpeg
is installed, independent of the MCP SDK. Frame extraction uses a fast approximate
keyframe seek (`-ss` before `-i`) and emits PNG to stdout — no temp files
(design §5.4).
"""

from __future__ import annotations

import json
import re
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
    cmd = [
        "ffprobe",
        "-loglevel",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(p),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(_timeout_msg("ffprobe", timeout)) from e
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


def probe(p: Path, timeout: Optional[float] = None) -> "tuple[float, float]":
    """Return ``(duration_seconds, fps)`` from a single ffprobe call.

    ``fps`` is 0.0 when there is no video stream or no reported frame rate. The frame
    tools use it to keep the last evenly-spaced sample about a frame inside the end —
    a fast seek (`-ss` before `-i`) past the final frame's timestamp returns nothing.
    """
    info = ffprobe(p, timeout)
    dur = info.get("format", {}).get("duration")
    fps = 0.0
    for s in info.get("streams", []):
        if dur is None and s.get("duration"):
            dur = s["duration"]
        if s.get("codec_type") == "video" and not fps:
            num, _, den = (s.get("avg_frame_rate") or "0/0").partition("/")
            try:
                fps = int(num) / int(den) if int(den) else 0.0
            except ValueError:
                fps = 0.0
    return float(dur) if dur else 0.0, fps


def norm_format(fmt: str) -> str:
    """Normalize a caller format string to 'png' or 'jpeg'."""
    f = (fmt or "png").lower()
    return "jpeg" if f in ("jpg", "jpeg") else "png"


def _jpeg_qscale(quality: int) -> int:
    """Map a 1..100 quality (higher=better) to ffmpeg mjpeg -q:v (2=best..31=worst)."""
    q = max(1, min(100, int(quality)))
    return max(2, min(31, round(2 + (100 - q) * (31 - 2) / 99)))


def extract_frame(
    p: Path,
    t: float,
    max_dim: Optional[int],
    fmt: str = "png",
    quality: int = 85,
    timeout: Optional[float] = None,
) -> bytes:
    """Grab one frame at time `t` (seconds). Fast keyframe seek (-ss before -i).

    `fmt` is 'png' (lossless) or 'jpeg' (far smaller; `quality` 1..100 applies).
    `timeout` (seconds, None = unbounded) caps extraction so a pathological file
    cannot hang the server.
    """
    fmt = norm_format(fmt)
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-ss",
        f"{max(t, 0):.3f}",
        "-i",
        str(p),
        "-frames:v",
        "1",
    ]
    if max_dim:
        cmd += ["-vf", f"scale='min({int(max_dim)},iw)':-2"]
    if fmt == "jpeg":
        cmd += ["-c:v", "mjpeg", "-q:v", str(_jpeg_qscale(quality))]
    else:
        cmd += ["-c:v", "png"]
    cmd += ["-f", "image2", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(_timeout_msg("ffmpeg", timeout)) from e
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError(
            res.stderr.decode(errors="replace").strip()[:300]
            or "ffmpeg produced no frame"
        )
    return res.stdout


_PTS_TIME = re.compile(r"pts_time:([0-9]+\.?[0-9]*)")


def size_scaled_timeout(p: Path, seconds_per_gb: float = 30.0) -> float:
    """A decode-time budget scaled by file size, for heavy full-decode ops like scene
    detection: ``seconds_per_gb`` per gigabyte, never less than one GB's worth so a
    small file still gets a usable floor (≈30s for 1 GB, ≈60s for 2 GB).

    Scaling by bytes is a rough proxy — real decode cost tracks frame count
    (duration x fps) more than size — so the floor mainly guards small-but-long clips.
    """
    try:
        gb = p.stat().st_size / 1_000_000_000
    except OSError:
        gb = 0.0
    return seconds_per_gb * max(1.0, gb)


def detect_scenes(
    p: Path,
    threshold: float = 0.4,
    timeout: Optional[float] = None,
    scale_width: int = 320,
) -> "list[float]":
    """Return shot/scene-cut start times (seconds), always including 0.0.

    Decodes the whole file applying ``select='gt(scene,threshold)'`` and prints the
    matching frames' timestamps via the ``metadata`` filter; we parse ``pts_time`` from
    the log. Frames are downscaled first (purely to speed up detection — the scene
    score doesn't need full resolution). ``threshold`` is 0..1 (lower = more cuts).

    Note: this is a full-decode pass, so it is the heaviest video op; it is bounded by
    `timeout` like the others.
    """
    thr = max(0.0, min(1.0, float(threshold)))
    # Escape the comma inside gt() so it isn't read as a filter separator.
    vf = f"scale={int(scale_width)}:-2,select='gt(scene\\,{thr})',metadata=print"
    cmd = [
        "ffmpeg",
        "-loglevel",
        "info",
        "-i",
        str(p),
        "-an",
        "-sn",
        "-filter:v",
        vf,
        "-f",
        "null",
        "-",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Scene detection timed out after {timeout:g}s — the video is long/large "
            f"for its size-scaled budget. Try a shorter clip, or an operator can lift "
            f"the limit with --ffmpeg-timeout 0."
        ) from e
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300] or "ffmpeg scene detection failed")
    # metadata=print logs "... pts_time:<seconds>" for each selected (cut) frame.
    times = {0.0}
    for m in _PTS_TIME.finditer(res.stderr):
        t = round(float(m.group(1)), 3)
        if t > 0:
            times.add(t)
    return sorted(times)
