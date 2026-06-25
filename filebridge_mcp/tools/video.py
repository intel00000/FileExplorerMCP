"""Video tools — the vision-channel half of the server.

  video_info            probe duration/streams to plan timestamps
  video_frame           one frame; seek by seconds OR by percent of duration
  video_frames          a set of frames: evenly across a slice, or at explicit timestamps
  video_contact_sheet   N frames tiled into ONE labeled image (token-efficient overview)

Cross-cutting options on the frame tools:
  format / quality  'png' (lossless) or 'jpeg' (much smaller; quality 1..100)
  output            'inline' returns image content; 'file' writes the frame(s) under
                    the frames dir and returns PATHS instead. The 'file' mode is the
                    §9 mtmd fallback: when a host can't route inline base64 into the
                    vision encoder, it attaches the saved file as a real image instead.

Every inline image block carries `_meta` ({path, timestamp_sec, frame_index, ...})
so the host can isolate AND identify a frame without depending on block order; a
frame that fails to extract emits a JSON error block tagged with the same index.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated, Optional

from pydantic import Field

from .. import deps
from ..config import DEFAULT_FRAMES, MAX_FRAMES, PDF_RENDER_DIM, RO, resolve_dim
from ..media import compose
from ..media.images import image_content
from ..media.video import (
    detect_scenes,
    duration,
    extract_frame,
    ffprobe,
    norm_format,
    probe,
    size_scaled_timeout,
)
from ..sandbox import Root

_FMT = Field(
    description="Frame encoding: 'png' (lossless) or 'jpeg' (smaller). Omit to use the "
    "server default (--image-format), else png (frames) / jpeg (contact sheet).",
    pattern="^(png|jpe?g)$",
)
_QUALITY = Field(
    description="JPEG quality 1..100 (higher=better); ignored for png. Omit to use the "
    "server default (--image-quality), else 85.",
    ge=1,
    le=100,
)
_OUTPUT = Field(
    description="'inline' returns image content; 'file' writes it and returns a path.",
    pattern="^(inline|file)$",
)
_MAXDIM = Field(
    description="Cap the longest edge of the frame (px). Omit for no cap (native size). "
    "The effective cap is the smaller of this and any server-set --max-image-dimension.",
    ge=64,
    le=8192,
)

# A fast seek (`-ss` before `-i`) past the final frame's timestamp returns no frame,
# so bound any sample by a margin about 1.5 frames before the end.
_SEEK_FALLBACK_MARGIN = 0.5


def _clamp_seek(t: float, dur: float, fps: float = 0.0) -> float:
    """Clamp a seek time so it lands on a real frame (ms-rounded)."""
    if dur and dur > 0:
        margin = (1.5 / fps) if fps and fps > 0 else _SEEK_FALLBACK_MARGIN
        t = min(t, max(0.0, dur - margin))
    return round(max(0.0, t), 3)


def register(
    mcp,
    root: Root,
    *,
    frames_dir: Optional[Path] = None,
    max_image_dim: Optional[int] = None,
    image_format: Optional[str] = None,
    image_quality: Optional[int] = None,
    ffmpeg_timeout: Optional[float] = None,
    frames_ttl: Optional[float] = None,
) -> None:
    cache_dir = (
        frames_dir if frames_dir is not None else (root.base / ".filebridge_frames")
    )

    # output='file' artifacts are named with these prefixes so the TTL sweep can
    # recognize and reclaim *our* files without touching unrelated files an operator
    # may keep in a custom --frames-dir.
    _FRAME_PREFIX = "fbframe_"
    _SHEET_PREFIX = "fbsheet_"

    def _sweep_frames() -> None:
        """Best-effort cleanup of previously written frame/sheet files older than the
        TTL, so output='file' artifacts don't pile up over a long session.

        Only our own prefixed files are considered, and only those older than the TTL —
        a frame just handed to the host to attach on the next turn (seconds old) is
        never removed. A falsy or non-positive frames_ttl disables the sweep.
        """
        if not frames_ttl or frames_ttl <= 0 or not cache_dir.exists():
            return
        cutoff = time.time() - frames_ttl
        for prefix in (_FRAME_PREFIX, _SHEET_PREFIX):
            for f in cache_dir.glob(prefix + "*"):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass

    def _save_frame(data: bytes, src: Path, t: float, fmt: str) -> str:
        """Write a frame under the frames dir; return its path (relative to root)."""
        ext = "jpg" if fmt == "jpeg" else "png"
        stem = root.rel(src).replace("/", "_")
        stem = stem.rsplit(".", 1)[0] if "." in stem else stem
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{_FRAME_PREFIX}{stem}_{t:09.3f}.{ext}"
        out.write_bytes(data)
        return root.rel(out)

    @mcp.tool(name="video_info", annotations={"title": "Probe video/audio", **RO})
    def video_info(
        path: Annotated[str, Field(description="Media file relative to root")],
    ) -> str:
        """Probe a media file so the model knows the time range it can sample.

        Returns JSON: {"path","duration_sec","width","height","fps","codec",
        "has_audio"}. Call this before video_frames to pick sensible timestamps.
        """
        err = deps.require_ffmpeg()
        if err:
            return json.dumps({"error": err})
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        try:
            info = ffprobe(p, ffmpeg_timeout)
        except RuntimeError as e:
            return json.dumps({"error": root.scrub(str(e))})
        out = {
            "path": root.rel(p),
            "duration_sec": round(duration(p, ffmpeg_timeout), 3),
            "width": None,
            "height": None,
            "fps": None,
            "codec": None,
            "has_audio": False,
        }
        for s in info.get("streams", []):
            if s.get("codec_type") == "video" and out["codec"] is None:
                out.update(
                    width=s.get("width"),
                    height=s.get("height"),
                    codec=s.get("codec_name"),
                )
                rate = s.get("avg_frame_rate", "0/0")
                try:
                    num, den = rate.split("/")
                    out["fps"] = round(int(num) / int(den), 3) if int(den) else None
                except (ValueError, ZeroDivisionError):
                    pass
            if s.get("codec_type") == "audio":
                out["has_audio"] = True
        return json.dumps(out, indent=2)

    @mcp.tool(name="video_scenes", annotations={"title": "Detect scene cuts", **RO})
    def video_scenes(
        path: Annotated[str, Field(description="Video file relative to root")],
        threshold: Annotated[
            float,
            Field(
                description="Scene-change sensitivity 0..1; lower finds more cuts "
                "(~0.3-0.4 typical).",
                ge=0.0,
                le=1.0,
            ),
        ] = 0.4,
        max_scenes: Annotated[
            int, Field(description="Cap on cuts returned", ge=1, le=1000)
        ] = 200,
    ) -> str:
        """Find shot/scene-cut timestamps so you can sample where the picture actually
        changes instead of blindly.

        Returns JSON {"path","duration_sec","scene_count","truncated",
        "scenes":[{"index","start_sec"}]}, always including 0.0 as the first cut. Feed
        the start_sec values into video_frames(timestamps=...), or tile them with
        video_contact_sheet, for one representative frame per shot. This is a full
        decode pass (the heaviest video op); its time budget scales with file size
        (~30s/GB) and frames are downscaled internally to speed it up.
        """
        err = deps.require_ffmpeg()
        if err:
            return json.dumps({"error": err})
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        # Scene detection is a full-decode pass; budget it by file size (~30s/GB)
        # instead of the flat per-call ffmpeg timeout. A disabled timeout (None)
        # stays unbounded.
        scene_timeout = None if ffmpeg_timeout is None else size_scaled_timeout(p)
        try:
            cuts = detect_scenes(p, threshold, scene_timeout)
            dur, _ = probe(p, ffmpeg_timeout)
        except RuntimeError as e:
            return json.dumps({"error": root.scrub(str(e))})
        truncated = len(cuts) > max_scenes
        scenes = [{"index": i, "start_sec": t} for i, t in enumerate(cuts[:max_scenes])]
        return json.dumps(
            {
                "path": root.rel(p),
                "duration_sec": round(dur, 3),
                "scene_count": len(scenes),
                "truncated": truncated,
                "scenes": scenes,
            },
            indent=2,
        )

    @mcp.tool(
        name="video_frame", annotations={"title": "Extract one video frame", **RO}
    )
    def video_frame(
        path: Annotated[str, Field(description="Video file relative to root")],
        timestamp: Annotated[
            Optional[float], Field(description="Seek position in seconds.", ge=0)
        ] = None,
        percent: Annotated[
            Optional[float],
            Field(
                description="Seek to this %% of the duration (e.g. 60 = 60%% mark). Overrides timestamp.",
                ge=0,
                le=100,
            ),
        ] = None,
        max_dimension: Annotated[Optional[int], _MAXDIM] = None,
        format: Annotated[Optional[str], _FMT] = None,
        quality: Annotated[Optional[int], _QUALITY] = None,
        output: Annotated[str, _OUTPUT] = "inline",
    ):
        """Return a single frame, by absolute `timestamp` (seconds) or by `percent`.

        Use for agentic seeking — probe with video_info, then narrow toward the moment
        (e.g. binary-search a title card), or jump straight to `percent=60`. Returns an
        Image (output='inline') or JSON with a frame_path (output='file'), or a JSON error.
        """
        err = deps.require_ffmpeg()
        if err:
            return json.dumps({"error": err})
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        fmt = norm_format(format or image_format or "png")
        q = quality or image_quality or 85
        try:
            if percent is not None:
                dur, fps = probe(p, ffmpeg_timeout)
                t = _clamp_seek(max(0.0, min(100.0, percent)) / 100.0 * dur, dur, fps)
            elif timestamp is not None:
                t = timestamp
            else:
                return json.dumps({
                    "error": "Provide timestamp (seconds) or percent (0-100)."
                })
            data = extract_frame(
                p, t, resolve_dim(max_dimension, max_image_dim), fmt, q, ffmpeg_timeout
            )
        except Exception as e:
            return json.dumps({"error": root.scrub(f"Frame extraction failed: {e}")})
        if output == "file":
            _sweep_frames()
            return json.dumps(
                {
                    "path": root.rel(p),
                    "timestamp_sec": round(t, 3),
                    "frame_path": _save_frame(data, p, t, fmt),
                    "format": fmt,
                },
                indent=2,
            )
        return image_content(
            data, fmt, {"path": root.rel(p), "timestamp_sec": round(t, 3)}
        )

    @mcp.tool(
        name="video_frames", annotations={"title": "Sweep or sample video frames", **RO}
    )
    def video_frames(
        path: Annotated[str, Field(description="Video file relative to root")],
        start: Annotated[
            float, Field(description="Sweep start in seconds (sweep mode)", ge=0)
        ] = 0.0,
        step: Annotated[
            float, Field(description="Seconds between frames in sweep mode", gt=0)
        ] = 1.0,
        count: Annotated[
            int,
            Field(
                description="How many frames to return this call", ge=1, le=MAX_FRAMES
            ),
        ] = DEFAULT_FRAMES,
        timestamps: Annotated[
            Optional[list[float]],
            Field(
                description="Explicit seconds to grab. When set, overrides the sweep "
                "(start/step are ignored)."
            ),
        ] = None,
        max_dimension: Annotated[Optional[int], _MAXDIM] = None,
        format: Annotated[Optional[str], _FMT] = None,
        quality: Annotated[Optional[int], _QUALITY] = None,
        output: Annotated[str, _OUTPUT] = "inline",
    ):
        """Get several frames at once, in one of two modes:

          sweep (default) -- walk the video from `start`, one frame every `step`
            seconds, `count` frames per call. The result carries a `next_offset` (the
            `start` for the next page, or null at the end), so you can march through a
            whole video a window at a time. Pair with ephemeral=true: note what you see,
            then page on with start=next_offset.
          explicit -- pass `timestamps=[...]` to grab those exact moments (e.g. the
            scene cuts from video_scenes, or points picked off a contact sheet).

        Ordered output. output='inline' returns a JSON summary block first, then the
        images; output='file' returns one JSON object listing each saved frame path.
        Both include `next_offset`.
        """
        err = deps.require_ffmpeg()
        if err:
            return [err] if output == "inline" else err
        p = root.resolve(path)
        if not p.is_file():
            msg = json.dumps({"error": f"Not a file: {path}"})
            return [msg] if output == "inline" else msg
        fmt = norm_format(format or image_format or "png")
        q = quality or image_quality or 85
        dim = resolve_dim(max_dimension, max_image_dim)
        try:
            dur, fps = probe(p, ffmpeg_timeout)
        except RuntimeError as e:
            msg = json.dumps({"error": root.scrub(str(e))})
            return [msg] if output == "inline" else msg

        next_offset = None
        if timestamps:
            stamps = [_clamp_seek(float(t), dur, fps) for t in timestamps[:MAX_FRAMES]]
        else:
            if dur and start >= dur:
                msg = json.dumps({
                    "error": f"start={start} is at/after duration={round(dur, 3)}"
                })
                return [msg] if output == "inline" else msg
            stamps = []
            for i in range(count):
                t = start + i * step
                if dur and t >= dur:
                    break  # walked past the end of the video
                stamps.append(_clamp_seek(t, dur, fps))
            nxt = round(start + count * step, 3)
            next_offset = nxt if (dur and nxt < dur) else None

        if output == "file":
            _sweep_frames()
            frames = []
            for t in stamps:
                try:
                    frames.append({
                        "timestamp_sec": t,
                        "frame_path": _save_frame(
                            extract_frame(p, t, dim, fmt, q, ffmpeg_timeout), p, t, fmt
                        ),
                    })
                except Exception as e:
                    frames.append({"timestamp_sec": t, "error": root.scrub(str(e))})
            return json.dumps(
                {
                    "path": root.rel(p),
                    "format": fmt,
                    "frames": frames,
                    "next_offset": next_offset,
                },
                indent=2,
            )

        results: list = [
            json.dumps({
                "path": root.rel(p),
                "timestamps_sec": stamps,
                "next_offset": next_offset,
            })
        ]
        for i, t in enumerate(stamps):
            try:
                data = extract_frame(p, t, dim, fmt, q, ffmpeg_timeout)
                results.append(
                    image_content(
                        data,
                        fmt,
                        {"path": root.rel(p), "timestamp_sec": t, "frame_index": i},
                    )
                )
            except Exception as e:
                results.append(
                    json.dumps({
                        "error": root.scrub(f"frame at {t}s failed: {e}"),
                        "frame_index": i,
                        "timestamp_sec": t,
                    })
                )
        return results

    @mcp.tool(
        name="video_contact_sheet",
        annotations={"title": "Tile frames into one image", **RO},
    )
    def video_contact_sheet(
        path: Annotated[str, Field(description="Video file relative to root")],
        count: Annotated[
            int,
            Field(description="Frames to tile across the slice", ge=1, le=MAX_FRAMES),
        ] = 12,
        cols: Annotated[int, Field(description="Grid columns", ge=1, le=12)] = 4,
        start: Annotated[
            float, Field(description="Slice start in seconds", ge=0)
        ] = 0.0,
        end: Annotated[
            Optional[float],
            Field(description="Slice end in seconds; omit for end of video"),
        ] = None,
        max_dimension: Annotated[Optional[int], _MAXDIM] = None,
        format: Annotated[Optional[str], _FMT] = None,
        quality: Annotated[Optional[int], _QUALITY] = None,
        output: Annotated[str, _OUTPUT] = "inline",
    ):
        """Tile `count` evenly-spaced, timestamp-labeled frames into ONE image.

        A single composite costs far fewer vision tokens than `count` separate frames —
        ideal for a first-pass overview of a clip before zooming in with video_frame.
        Returns an Image (inline) or a saved sheet path (file). Needs Pillow.
        """
        err = deps.require_ffmpeg()
        if err:
            return json.dumps({"error": err})
        if not compose.available():
            return json.dumps({
                "error": "Contact sheet needs Pillow. Run: pip install pillow"
            })
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        fmt = norm_format(format or image_format or "jpeg")
        q = quality or image_quality or 85
        # A contact sheet must have a bounded overall size, so an uncapped (None)
        # request falls back to a sane composite size rather than tiling native frames.
        dim = resolve_dim(max_dimension, max_image_dim) or PDF_RENDER_DIM
        try:
            dur, fps = probe(p, ffmpeg_timeout)
        except RuntimeError as e:
            return json.dumps({"error": root.scrub(str(e))})
        hi = dur if end is None else min(end, dur)
        if hi <= start:
            return json.dumps({
                "error": f"Empty slice: start={start}, end={hi}, duration={round(dur, 3)}"
            })
        if count == 1:
            stamps = [_clamp_seek(start, dur, fps)]
        else:
            step = (hi - start) / (count - 1)
            stamps = [_clamp_seek(start + i * step, dur, fps) for i in range(count)]

        tile_w = max(64, dim // max(1, cols))
        tiles = []
        for t in stamps:
            try:
                tiles.append((
                    f"{t:.2f}s",
                    extract_frame(p, t, tile_w, "png", timeout=ffmpeg_timeout),
                ))  # png tiles for clean compositing
            except Exception:
                continue
        if not tiles:
            return json.dumps({
                "error": "No frames could be extracted for the contact sheet."
            })
        try:
            sheet = compose.contact_sheet(tiles, cols, dim, fmt, q)
        except RuntimeError as e:
            return json.dumps({"error": root.scrub(str(e))})

        if output == "file":
            _sweep_frames()
            stem = root.rel(p).replace("/", "_").rsplit(".", 1)[0]
            cache_dir.mkdir(parents=True, exist_ok=True)
            out = (
                cache_dir
                / f"{_SHEET_PREFIX}{stem}_sheet_{len(tiles)}.{'jpg' if fmt == 'jpeg' else 'png'}"
            )
            out.write_bytes(sheet)
            return json.dumps(
                {
                    "path": root.rel(p),
                    "frame_count": len(tiles),
                    "cols": cols,
                    "sheet_path": root.rel(out),
                    "format": fmt,
                },
                indent=2,
            )
        return image_content(
            sheet,
            fmt,
            {
                "path": root.rel(p),
                "kind": "contact_sheet",
                "frame_count": len(tiles),
                "cols": cols,
            },
        )
