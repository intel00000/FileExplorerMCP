"""Video-tool tests through the MCP call path. Needs the SDK + ffmpeg/ffprobe."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess

import pytest

pytest.importorskip("mcp")
if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("ffmpeg/ffprobe not on PATH", allow_module_level=True)

from filebridge_mcp.sandbox import Root  # noqa: E402
from filebridge_mcp.server import build_server  # noqa: E402

# Contact-sheet composition needs Pillow; skip those tests cleanly without it.
needs_pil = pytest.mark.skipif(
    __import__("importlib.util", fromlist=["util"]).find_spec("PIL") is None,
    reason="contact sheet needs Pillow",
)


@pytest.fixture
def video_root(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=3:size=320x240:rate=10",
            str(clip),
            "-y",
        ],
        check=True,
    )
    return tmp_path


def _blk(res):
    return res[0] if isinstance(res, tuple) else res


def _call(mcp, name, **kw):
    return _blk(asyncio.run(mcp.call_tool(name, kw)))


def _images(blocks):
    return [c for c in blocks if type(c).__name__ == "ImageContent"]


def _raw(image_block) -> bytes:
    return base64.b64decode(image_block.data)


def test_percent_seek_returns_image(video_root):
    mcp = build_server(Root(video_root))
    assert len(_images(_call(mcp, "video_frame", path="clip.mp4", percent=50))) == 1


def test_frame_meta_identifies_image(video_root):
    mcp = build_server(Root(video_root))
    img = _images(_call(mcp, "video_frame", path="clip.mp4", timestamp=1.5))[0]
    assert img.meta == {"path": "clip.mp4", "timestamp_sec": 1.5}


def test_frames_meta_carries_index_and_timestamp(video_root):
    mcp = build_server(Root(video_root))
    imgs = _images(_call(mcp, "video_frames", path="clip.mp4", timestamps=[0.5, 1.5]))
    assert [i.meta["frame_index"] for i in imgs] == [0, 1]
    assert [i.meta["timestamp_sec"] for i in imgs] == [0.5, 1.5]
    assert all(i.meta["path"] == "clip.mp4" for i in imgs)


@needs_pil
def test_contact_sheet_meta(video_root):
    mcp = build_server(Root(video_root))
    img = _images(_call(mcp, "video_contact_sheet", path="clip.mp4", count=4, cols=2))[
        0
    ]
    assert img.meta["kind"] == "contact_sheet"
    assert img.meta["cols"] == 2 and img.meta["path"] == "clip.mp4"


def test_frame_requires_timestamp_or_percent(video_root):
    mcp = build_server(Root(video_root))
    payload = json.loads(_call(mcp, "video_frame", path="clip.mp4")[0].text)
    assert "error" in payload


def test_jpeg_is_smaller_than_png(video_root):
    mcp = build_server(Root(video_root))
    png = _raw(
        _images(_call(mcp, "video_frame", path="clip.mp4", timestamp=1, format="png"))[
            0
        ]
    )
    jpg = _raw(
        _images(
            _call(
                mcp,
                "video_frame",
                path="clip.mp4",
                timestamp=1,
                format="jpeg",
                quality=60,
            )
        )[0]
    )
    assert len(jpg) < len(png)


def test_cli_image_format_default_and_model_override(video_root):
    # CLI sets jpeg as the default encoding for frames...
    mcp = build_server(Root(video_root), image_format="jpeg")
    assert (
        _images(_call(mcp, "video_frame", path="clip.mp4", timestamp=1))[0].mimeType
        == "image/jpeg"
    )
    # ...and the model can still override it per call.
    assert (
        _images(_call(mcp, "video_frame", path="clip.mp4", timestamp=1, format="png"))[
            0
        ].mimeType
        == "image/png"
    )


def test_explicit_timestamps(video_root):
    mcp = build_server(Root(video_root))
    blocks = _call(mcp, "video_frames", path="clip.mp4", timestamps=[0.5, 1.5, 2.5])
    assert len(_images(blocks)) == 3


def test_output_file_writes_frames_under_root(video_root):
    mcp = build_server(Root(video_root))
    payload = json.loads(
        _call(mcp, "video_frames", path="clip.mp4", timestamps=[1.0], output="file")[
            0
        ].text
    )
    rel = payload["frames"][0]["frame_path"]
    assert (video_root / rel).exists()
    assert rel.startswith(".filebridge_frames/")


def test_frames_dir_override(video_root, tmp_path):
    custom = tmp_path / "elsewhere" / "frames"
    mcp = build_server(Root(video_root), frames_dir=custom)
    payload = json.loads(
        _call(mcp, "video_frame", path="clip.mp4", timestamp=1, output="file")[0].text
    )
    # frame_path is absolute (outside root) and the file exists in the custom dir
    assert custom.exists() and any(custom.iterdir())


@needs_pil
def test_contact_sheet_returns_single_image(video_root):
    mcp = build_server(Root(video_root))
    blocks = _call(mcp, "video_contact_sheet", path="clip.mp4", count=4, cols=2)
    assert len(_images(blocks)) == 1


@needs_pil
def test_contact_sheet_output_file(video_root):
    mcp = build_server(Root(video_root))
    payload = json.loads(
        _call(
            mcp, "video_contact_sheet", path="clip.mp4", count=4, cols=2, output="file"
        )[0].text
    )
    assert (video_root / payload["sheet_path"]).exists()


def test_frames_sweep_paginates_with_cursor(video_root):
    """Default sweep: walk the clip by `step`, with a next_offset cursor for paging."""
    mcp = build_server(Root(video_root))
    blocks = _call(mcp, "video_frames", path="clip.mp4", start=0.0, step=0.5, count=4)
    assert len(_images(blocks)) == 4
    summary = json.loads(blocks[0].text)
    assert [round(t, 1) for t in summary["timestamps_sec"]] == [0.0, 0.5, 1.0, 1.5]
    assert summary["next_offset"] == 2.0


def test_frames_sweep_stops_at_end_with_null_cursor(video_root):
    """Sweeping past the 3s end yields a partial page, a null cursor, and no errors."""
    mcp = build_server(Root(video_root))
    blocks = _call(mcp, "video_frames", path="clip.mp4", start=2.0, step=1.0, count=5)
    assert len(_images(blocks)) >= 1
    summary = json.loads(blocks[0].text)
    assert summary["next_offset"] is None
    errs = [
        b
        for b in blocks
        if type(b).__name__ == "TextContent" and "frame at" in getattr(b, "text", "")
    ]
    assert errs == []


def test_video_scenes_detects_hard_cut(tmp_path):
    """video_scenes finds a hard cut and always includes 0.0 as the first scene."""
    clip = tmp_path / "cut.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=2:size=320x240:rate=10",
            "-f",
            "lavfi",
            "-i",
            "mandelbrot=size=320x240:rate=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-t",
            "4",
            str(clip),
            "-y",
        ],
        check=True,
    )
    mcp = build_server(Root(tmp_path))
    payload = json.loads(_call(mcp, "video_scenes", path="cut.mp4")[0].text)
    starts = [s["start_sec"] for s in payload["scenes"]]
    assert starts[0] == 0.0
    assert any(abs(t - 2.0) < 0.3 for t in starts)
    assert payload["scene_count"] == len(payload["scenes"])


def test_percent_100_returns_image(video_root):
    """video_frame(percent=100) used to seek exactly at duration and fail (BUG 2)."""
    mcp = build_server(Root(video_root))
    assert len(_images(_call(mcp, "video_frame", path="clip.mp4", percent=100))) == 1


@needs_pil
def test_contact_sheet_over_whole_clip_keeps_all_tiles(video_root):
    """The final tile (at t==duration) used to be silently dropped (BUG 2)."""
    mcp = build_server(Root(video_root))
    img = _images(_call(mcp, "video_contact_sheet", path="clip.mp4", count=6, cols=3))[
        0
    ]
    assert img.meta["frame_count"] == 6
