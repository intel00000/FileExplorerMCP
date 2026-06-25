"""Video tools: video_info (probe), video_frame (agentic seek), video_frames (slice).

The vision-channel half of the server. `video_frame` returns one image for
binary-searching toward a moment; `video_frames` returns an ordered set across a
time slice (the first list element is a JSON summary of the timestamps, the rest
are images). All three degrade to an actionable error when ffmpeg is absent.
"""

from __future__ import annotations

import json
from typing import Annotated, Optional

from pydantic import Field

from .. import deps
from ..config import DEFAULT_FRAMES, DEFAULT_MAX_DIM, MAX_FRAMES, RO
from ..media.video import duration, extract_frame, ffprobe
from ..sandbox import Root


def register(mcp, root: Root) -> None:
    @mcp.tool(name="video_info", annotations={"title": "Probe video/audio", **RO})
    def video_info(path: Annotated[str, Field(description="Media file relative to root")]) -> str:
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
        out = {"path": root.rel(p), "duration_sec": round(duration(p), 3),
               "width": None, "height": None, "fps": None, "codec": None, "has_audio": False}
        for s in info.get("streams", []):
            if s.get("codec_type") == "video" and out["codec"] is None:
                out.update(width=s.get("width"), height=s.get("height"), codec=s.get("codec_name"))
                rate = s.get("avg_frame_rate", "0/0")
                try:
                    num, den = rate.split("/")
                    out["fps"] = round(int(num) / int(den), 3) if int(den) else None
                except (ValueError, ZeroDivisionError):
                    pass
            if s.get("codec_type") == "audio":
                out["has_audio"] = True
        return json.dumps(out, indent=2)

    @mcp.tool(name="video_frame", annotations={"title": "Extract one video frame", **RO})
    def video_frame(
        path: Annotated[str, Field(description="Video file relative to root")],
        timestamp: Annotated[float, Field(description="Seek position in seconds", ge=0)],
        max_dimension: Annotated[int, Field(description="Cap longest edge of the frame (px)", ge=64, le=4096)] = DEFAULT_MAX_DIM,
    ):
        """Return a single frame at `timestamp` as an image the model can see.

        Use for agentic seeking — probe with video_info, then narrow toward the moment
        you want (e.g. binary-search for a title card). Returns an Image, or a JSON
        error string.
        """
        err = deps.require_ffmpeg()
        if err:
            return json.dumps({"error": err})
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        from mcp.server.fastmcp import Image
        try:
            return Image(data=extract_frame(p, timestamp, max_dimension), format="png")
        except Exception as e:
            return json.dumps({"error": f"Frame extraction failed: {e}"})

    @mcp.tool(name="video_frames", annotations={"title": "Sample frames across a slice", **RO})
    def video_frames(
        path: Annotated[str, Field(description="Video file relative to root")],
        start: Annotated[float, Field(description="Slice start in seconds", ge=0)] = 0.0,
        end: Annotated[Optional[float], Field(description="Slice end in seconds; omit for end of video")] = None,
        count: Annotated[int, Field(description="Number of frames evenly sampled across the slice", ge=1, le=MAX_FRAMES)] = DEFAULT_FRAMES,
        max_dimension: Annotated[int, Field(description="Cap longest edge of each frame (px)", ge=64, le=4096)] = DEFAULT_MAX_DIM,
    ):
        """Evenly sample `count` frames across the time slice [start, end] as images.

        This is the slice-access view of a video: one call returns an ordered set of
        frames so the model can scan a span. The first list element is a JSON summary
        naming the timestamp of each frame in order; the rest are the frame images.
        Keep count modest — every frame is encoded by the vision model in full.
        """
        err = deps.require_ffmpeg()
        if err:
            return [err]
        p = root.resolve(path)
        if not p.is_file():
            return [json.dumps({"error": f"Not a file: {path}"})]
        from mcp.server.fastmcp import Image
        try:
            dur = duration(p)
        except RuntimeError as e:
            return [json.dumps({"error": str(e)})]
        hi = dur if end is None else min(end, dur)
        if hi <= start:
            return [json.dumps({"error": f"Empty slice: start={start}, end={hi}, duration={round(dur, 3)}"})]
        if count == 1:
            stamps = [start]
        else:
            step = (hi - start) / (count - 1)
            stamps = [round(start + i * step, 3) for i in range(count)]
        results: list = [json.dumps({"path": root.rel(p), "slice": [start, round(hi, 3)],
                                     "timestamps_sec": stamps})]
        for t in stamps:
            try:
                results.append(Image(data=extract_frame(p, t, max_dimension), format="png"))
            except Exception as e:
                results.append(json.dumps({"error": f"frame at {t}s failed: {e}"}))
        return results
