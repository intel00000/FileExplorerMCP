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
from pathlib import Path
from typing import Annotated, Optional

from pydantic import Field

from .. import deps
from ..config import DEFAULT_FRAMES, MAX_FRAMES, PDF_RENDER_DIM, RO, resolve_dim
from ..media import compose
from ..media.images import image_content
from ..media.video import duration, extract_frame, ffprobe, norm_format
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


def register(
    mcp,
    root: Root,
    *,
    frames_dir: Optional[Path] = None,
    max_image_dim: Optional[int] = None,
    image_format: Optional[str] = None,
    image_quality: Optional[int] = None,
) -> None:
    cache_dir = (
        frames_dir if frames_dir is not None else (root.base / ".filebridge_frames")
    )

    def _save_frame(data: bytes, src: Path, t: float, fmt: str) -> str:
        """Write a frame under the frames dir; return its path (relative to root)."""
        ext = "jpg" if fmt == "jpeg" else "png"
        stem = root.rel(src).replace("/", "_")
        stem = stem.rsplit(".", 1)[0] if "." in stem else stem
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{stem}_{t:09.3f}.{ext}"
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
            info = ffprobe(p)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
        out = {
            "path": root.rel(p),
            "duration_sec": round(duration(p), 3),
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
                t = max(0.0, min(100.0, percent)) / 100.0 * duration(p)
            elif timestamp is not None:
                t = timestamp
            else:
                return json.dumps(
                    {"error": "Provide timestamp (seconds) or percent (0-100)."}
                )
            data = extract_frame(
                p, t, resolve_dim(max_dimension, max_image_dim), fmt, q
            )
        except Exception as e:
            return json.dumps({"error": f"Frame extraction failed: {e}"})
        if output == "file":
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
        name="video_frames", annotations={"title": "Sample frames across a slice", **RO}
    )
    def video_frames(
        path: Annotated[str, Field(description="Video file relative to root")],
        start: Annotated[
            float, Field(description="Slice start in seconds", ge=0)
        ] = 0.0,
        end: Annotated[
            Optional[float],
            Field(description="Slice end in seconds; omit for end of video"),
        ] = None,
        count: Annotated[
            int,
            Field(
                description="Frames evenly sampled across the slice",
                ge=1,
                le=MAX_FRAMES,
            ),
        ] = DEFAULT_FRAMES,
        timestamps: Annotated[
            Optional[list[float]],
            Field(description="Explicit seconds to grab (overrides start/end/count)."),
        ] = None,
        max_dimension: Annotated[Optional[int], _MAXDIM] = None,
        format: Annotated[Optional[str], _FMT] = None,
        quality: Annotated[Optional[int], _QUALITY] = None,
        output: Annotated[str, _OUTPUT] = "inline",
    ):
        """Sample frames as a set: evenly across [start, end], or at explicit `timestamps`.

        Ordered output. With output='inline', the first list element is a JSON summary
        of the timestamps and the rest are images. With output='file', a single JSON
        object lists each frame's saved path. Keep the frame count modest — every inline
        frame is encoded by the vision model in full.
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
            dur = duration(p)
        except RuntimeError as e:
            msg = json.dumps({"error": str(e)})
            return [msg] if output == "inline" else msg

        if timestamps:
            stamps = [
                round(max(0.0, min(float(t), dur)), 3) for t in timestamps[:MAX_FRAMES]
            ]
        else:
            hi = dur if end is None else min(end, dur)
            if hi <= start:
                msg = json.dumps(
                    {
                        "error": f"Empty slice: start={start}, end={hi}, duration={round(dur, 3)}"
                    }
                )
                return [msg] if output == "inline" else msg
            if count == 1:
                stamps = [round(start, 3)]
            else:
                step = (hi - start) / (count - 1)
                stamps = [round(start + i * step, 3) for i in range(count)]

        if output == "file":
            frames = []
            for t in stamps:
                try:
                    frames.append(
                        {
                            "timestamp_sec": t,
                            "frame_path": _save_frame(
                                extract_frame(p, t, dim, fmt, q), p, t, fmt
                            ),
                        }
                    )
                except Exception as e:
                    frames.append({"timestamp_sec": t, "error": str(e)})
            return json.dumps(
                {"path": root.rel(p), "format": fmt, "frames": frames}, indent=2
            )

        results: list = [json.dumps({"path": root.rel(p), "timestamps_sec": stamps})]
        for i, t in enumerate(stamps):
            try:
                data = extract_frame(p, t, dim, fmt, q)
                results.append(
                    image_content(
                        data,
                        fmt,
                        {"path": root.rel(p), "timestamp_sec": t, "frame_index": i},
                    )
                )
            except Exception as e:
                results.append(
                    json.dumps(
                        {
                            "error": f"frame at {t}s failed: {e}",
                            "frame_index": i,
                            "timestamp_sec": t,
                        }
                    )
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
            return json.dumps(
                {"error": "Contact sheet needs Pillow. Run: pip install pillow"}
            )
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        fmt = norm_format(format or image_format or "jpeg")
        q = quality or image_quality or 85
        # A contact sheet must have a bounded overall size, so an uncapped (None)
        # request falls back to a sane composite size rather than tiling native frames.
        dim = resolve_dim(max_dimension, max_image_dim) or PDF_RENDER_DIM
        try:
            dur = duration(p)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})
        hi = dur if end is None else min(end, dur)
        if hi <= start:
            return json.dumps(
                {
                    "error": f"Empty slice: start={start}, end={hi}, duration={round(dur, 3)}"
                }
            )
        if count == 1:
            stamps = [round(start, 3)]
        else:
            step = (hi - start) / (count - 1)
            stamps = [round(start + i * step, 3) for i in range(count)]

        tile_w = max(64, dim // max(1, cols))
        tiles = []
        for t in stamps:
            try:
                tiles.append(
                    (f"{t:.2f}s", extract_frame(p, t, tile_w, "png"))
                )  # png tiles for clean compositing
            except Exception:
                continue
        if not tiles:
            return json.dumps(
                {"error": "No frames could be extracted for the contact sheet."}
            )
        try:
            sheet = compose.contact_sheet(tiles, cols, dim, fmt, q)
        except RuntimeError as e:
            return json.dumps({"error": str(e)})

        if output == "file":
            stem = root.rel(p).replace("/", "_").rsplit(".", 1)[0]
            cache_dir.mkdir(parents=True, exist_ok=True)
            out = (
                cache_dir
                / f"{stem}_sheet_{len(tiles)}.{'jpg' if fmt == 'jpeg' else 'png'}"
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
