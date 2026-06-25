"""Server-assembly tests: tool gating and corrupt-file handling.

These need the MCP SDK; skipped automatically where it isn't installed so the
SDK-free core tests still run in a minimal environment.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from filebridge_mcp.sandbox import Root          # noqa: E402
from filebridge_mcp.server import build_server    # noqa: E402

WRITE_TOOLS = {"write_file", "make_dir"}
DELETE_TOOLS = {"move", "delete"}
MUTATING = WRITE_TOOLS | DELETE_TOOLS


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _blocks(res):
    """Normalize call_tool's return to the content-block list across SDK versions
    (some return ``list[ContentBlock]``, some ``(content, structured)``)."""
    return res[0] if isinstance(res, tuple) else res


def test_default_is_read_only(tmp_path):
    names = _tool_names(build_server(Root(tmp_path)))
    assert names.isdisjoint(MUTATING)
    assert {"list_dir", "read_file", "glob", "video_frame"} <= names


def test_allow_write_only(tmp_path):
    names = _tool_names(build_server(Root(tmp_path), allow_write=True))
    assert WRITE_TOOLS <= names
    assert names.isdisjoint(DELETE_TOOLS)


def test_allow_delete_only(tmp_path):
    names = _tool_names(build_server(Root(tmp_path), allow_delete=True))
    assert DELETE_TOOLS <= names
    assert names.isdisjoint(WRITE_TOOLS)


def test_allow_both_registers_full_catalog(tmp_path):
    names = _tool_names(build_server(Root(tmp_path), allow_write=True, allow_delete=True))
    assert MUTATING <= names
    assert len(names) == 14  # 10 read-only + 4 mutating


def test_corrupt_image_returns_error_not_crash(tmp_path):
    # Valid PNG magic bytes but no real image data — Pillow cannot decode it.
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    mcp = build_server(Root(tmp_path))
    content = _blocks(asyncio.run(mcp.call_tool("read_file", {"path": "broken.png"})))
    payload = json.loads(content[0].text)
    assert "error" in payload
    assert "broken.png" in payload["error"]


def test_write_blocked_when_not_allowed(tmp_path):
    mcp = build_server(Root(tmp_path))  # read-only
    with pytest.raises(Exception):  # tool isn't registered -> call fails
        asyncio.run(mcp.call_tool("write_file", {"path": "x.txt", "content": "hi"}))
    assert not (tmp_path / "x.txt").exists()


def test_read_image_stamps_meta(tmp_path):
    PILImage = pytest.importorskip("PIL.Image")
    PILImage.new("RGB", (32, 24), (10, 20, 30)).save(tmp_path / "pic.png", format="PNG")
    mcp = build_server(Root(tmp_path))
    blocks = _blocks(asyncio.run(mcp.call_tool("read_file", {"path": "pic.png"})))
    imgs = [b for b in blocks if b.type == "image"]
    assert len(imgs) == 1
    assert imgs[0].meta == {"path": "pic.png", "kind": "image"}


def test_read_pdf_render_page_stamps_meta(tmp_path):
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    doc.save(str(tmp_path / "doc.pdf"))
    doc.close()
    mcp = build_server(Root(tmp_path))
    blocks = _blocks(asyncio.run(mcp.call_tool("read_file", {"path": "doc.pdf", "render_page": True})))
    imgs = [b for b in blocks if b.type == "image"]
    assert len(imgs) == 1
    assert imgs[0].meta == {"path": "doc.pdf", "kind": "pdf_page", "page": 1}
