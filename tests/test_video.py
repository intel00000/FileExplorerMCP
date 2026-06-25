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

from filebridge_mcp.sandbox import Root        # noqa: E402
from filebridge_mcp.server import build_server  # noqa: E402


@pytest.fixture
def video_root(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=duration=3:size=320x240:rate=10", str(clip), "-y"],
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


def test_frame_requires_timestamp_or_percent(video_root):
    mcp = build_server(Root(video_root))
    payload = json.loads(_call(mcp, "video_frame", path="clip.mp4")[0].text)
    assert "error" in payload


def test_jpeg_is_smaller_than_png(video_root):
    mcp = build_server(Root(video_root))
    png = _raw(_images(_call(mcp, "video_frame", path="clip.mp4", timestamp=1, format="png"))[0])
    jpg = _raw(_images(_call(mcp, "video_frame", path="clip.mp4", timestamp=1, format="jpeg", quality=60))[0])
    assert len(jpg) < len(png)


def test_explicit_timestamps(video_root):
    mcp = build_server(Root(video_root))
    blocks = _call(mcp, "video_frames", path="clip.mp4", timestamps=[0.5, 1.5, 2.5])
    assert len(_images(blocks)) == 3


def test_output_file_writes_frames_under_root(video_root):
    mcp = build_server(Root(video_root))
    payload = json.loads(_call(mcp, "video_frames", path="clip.mp4", timestamps=[1.0], output="file")[0].text)
    rel = payload["frames"][0]["frame_path"]
    assert (video_root / rel).exists()
    assert rel.startswith(".filebridge_frames/")


def test_frames_dir_override(video_root, tmp_path):
    custom = tmp_path / "elsewhere" / "frames"
    mcp = build_server(Root(video_root), frames_dir=custom)
    payload = json.loads(_call(mcp, "video_frame", path="clip.mp4", timestamp=1, output="file")[0].text)
    # frame_path is absolute (outside root) and the file exists in the custom dir
    assert custom.exists() and any(custom.iterdir())


def test_contact_sheet_returns_single_image(video_root):
    mcp = build_server(Root(video_root))
    blocks = _call(mcp, "video_contact_sheet", path="clip.mp4", count=4, cols=2)
    assert len(_images(blocks)) == 1


def test_contact_sheet_output_file(video_root):
    mcp = build_server(Root(video_root))
    payload = json.loads(_call(mcp, "video_contact_sheet", path="clip.mp4", count=4, cols=2, output="file")[0].text)
    assert (video_root / payload["sheet_path"]).exists()
