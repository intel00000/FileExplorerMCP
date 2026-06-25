"""Server-assembly tests: tool gating and corrupt-file handling.

These need the MCP SDK; skipped automatically where it isn't installed so the
SDK-free core tests still run in a minimal environment.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from filebridge_mcp.sandbox import Root  # noqa: E402
from filebridge_mcp.server import build_server  # noqa: E402

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
    names = _tool_names(
        build_server(Root(tmp_path), allow_write=True, allow_delete=True)
    )
    assert MUTATING <= names
    assert len(names) == 14  # 10 read-only + 4 mutating


def test_corrupt_image_returns_error_not_crash(tmp_path):
    # Corruption is only detectable when Pillow actually decodes — i.e. when a cap is
    # active (downscale path). With no cap the file is passed through by path without
    # decoding, so there's nothing to detect. Use a cap here to exercise the decode path.
    pytest.importorskip("PIL")
    # Valid PNG magic bytes but no real image data — Pillow cannot decode it.
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    mcp = build_server(Root(tmp_path), max_image_dim=512)
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
    blocks = _blocks(
        asyncio.run(
            mcp.call_tool("read_file", {"path": "doc.pdf", "render_page": True})
        )
    )
    imgs = [b for b in blocks if b.type == "image"]
    assert len(imgs) == 1
    assert imgs[0].meta == {"path": "doc.pdf", "kind": "pdf_page", "page": 1}


def test_build_server_sets_host_and_port(tmp_path):
    mcp = build_server(Root(tmp_path), host="0.0.0.0", port=1234)
    assert (mcp.settings.host, mcp.settings.port) == ("0.0.0.0", 1234)


def test_build_server_sets_http_path(tmp_path):
    assert (
        build_server(Root(tmp_path), http_path="/").settings.streamable_http_path == "/"
    )
    assert build_server(Root(tmp_path)).settings.streamable_http_path == "/mcp"


def _read_image_size(mcp, name):
    import base64
    import io

    from PIL import Image as PILImage

    blocks = _blocks(asyncio.run(mcp.call_tool("read_file", {"path": name})))
    img = next(b for b in blocks if b.type == "image")
    return PILImage.open(io.BytesIO(base64.b64decode(img.data))).size


def _big_png(tmp_path):
    from PIL import Image as PILImage

    PILImage.new("RGB", (2000, 1000), (5, 5, 5)).save(
        tmp_path / "big.png", format="PNG"
    )


def test_no_cap_returns_native_resolution(tmp_path):
    pytest.importorskip("PIL.Image")
    _big_png(tmp_path)
    mcp = build_server(Root(tmp_path))  # no CLI cap; model omits max_dimension
    assert _read_image_size(mcp, "big.png") == (2000, 1000)  # native, uncapped


def test_model_cap_only(tmp_path):
    pytest.importorskip("PIL.Image")
    _big_png(tmp_path)
    mcp = build_server(Root(tmp_path))  # no CLI cap
    import base64
    import io

    from PIL import Image as PILImage

    blocks = _blocks(
        asyncio.run(
            mcp.call_tool("read_file", {"path": "big.png", "max_dimension": 128})
        )
    )
    img = next(b for b in blocks if b.type == "image")
    assert max(PILImage.open(io.BytesIO(base64.b64decode(img.data))).size) <= 128


def test_cli_cap_overrides_native(tmp_path):
    pytest.importorskip("PIL.Image")
    _big_png(tmp_path)
    mcp = build_server(Root(tmp_path), max_image_dim=256)  # CLI cap, model omits
    assert max(_read_image_size(mcp, "big.png")) <= 256


def test_cli_image_format_applies_and_model_overrides(tmp_path):
    PILImage = pytest.importorskip("PIL.Image")
    PILImage.new("RGB", (40, 30), (1, 2, 3)).save(tmp_path / "p.png", format="PNG")

    def mime(mcp, **kw):
        blocks = _blocks(
            asyncio.run(mcp.call_tool("read_file", {"path": "p.png", **kw}))
        )
        return next(b for b in blocks if b.type == "image").mimeType

    # No CLI default, model omits -> native (source is PNG).
    assert mime(build_server(Root(tmp_path))) == "image/png"
    # CLI default jpeg -> read_file image comes back jpeg.
    jpeg_srv = build_server(Root(tmp_path), image_format="jpeg")
    assert mime(jpeg_srv) == "image/jpeg"
    # Per-call format overrides the CLI default.
    assert mime(jpeg_srv, format="png") == "image/png"


def test_no_auth_by_default(tmp_path):
    assert build_server(Root(tmp_path)).settings.auth is None


def test_auth_token_enables_auth(tmp_path):
    mcp = build_server(Root(tmp_path), host="127.0.0.1", port=9000, auth_token="s3cret")
    assert mcp.settings.auth is not None


def test_static_token_verifier_accepts_and_rejects():
    from filebridge_mcp.auth import StaticTokenVerifier

    v = StaticTokenVerifier("good-secret")
    assert asyncio.run(v.verify_token("good-secret")) is not None
    assert asyncio.run(v.verify_token("wrong")) is None
    assert asyncio.run(v.verify_token("")) is None


def _props(tool):
    return (tool.inputSchema or {}).get("properties", {})


def test_every_tool_is_ephemeral_capable(tmp_path):
    # The global wrap (build_server) makes EVERY registered tool accept the shared
    # `ephemeral` field - including the mutating ones - with no per-tool decoration.
    mcp = build_server(Root(tmp_path), allow_write=True, allow_delete=True)
    tools = asyncio.run(mcp.list_tools())
    missing = [t.name for t in tools if "ephemeral" not in _props(t)]
    assert missing == [], f"tools missing ephemeral: {missing}"


def test_ephemeral_false_is_unchanged(tmp_path):
    from mcp.types import CallToolResult

    (tmp_path / "a.txt").write_text("hello\n")
    mcp = build_server(Root(tmp_path))
    res = asyncio.run(mcp.call_tool("list_dir", {"path": "."}))
    # No opt-in -> normal content path, no result-level _meta wrapper.
    assert not isinstance(res, CallToolResult)


def test_ephemeral_true_stamps_result_meta(tmp_path):
    from mcp.types import CallToolResult

    (tmp_path / "a.txt").write_text("hello\n")
    mcp = build_server(Root(tmp_path))
    res = asyncio.run(mcp.call_tool("list_dir", {"path": ".", "ephemeral": True}))
    assert isinstance(res, CallToolResult)
    assert res.meta == {"ephemeral": True}
    # Serializes under the wire key "_meta" the host reads.
    wire = res.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert wire["_meta"]["ephemeral"] is True
    # The listing itself still rides along as content.
    assert any(getattr(b, "type", None) == "text" for b in res.content)


def test_ephemeral_image_keeps_both_meta_layers(tmp_path):
    from mcp.types import CallToolResult

    PILImage = pytest.importorskip("PIL.Image")
    PILImage.new("RGB", (16, 16), (1, 2, 3)).save(tmp_path / "pic.png", format="PNG")
    mcp = build_server(Root(tmp_path))
    res = asyncio.run(
        mcp.call_tool("read_file", {"path": "pic.png", "ephemeral": True})
    )
    assert isinstance(res, CallToolResult)
    # Result-level (ephemeral) and block-level (identity) _meta coexist.
    assert res.meta == {"ephemeral": True}
    imgs = [b for b in res.content if b.type == "image"]
    assert len(imgs) == 1
    assert imgs[0].meta == {"path": "pic.png", "kind": "image"}


def test_list_dir_does_not_follow_escaping_symlink(tmp_path):
    """list_dir must not enumerate a directory reached through a symlink that
    escapes the root (design D5 containment, mirroring glob/grep)."""
    root_dir = tmp_path / "root"
    outside = tmp_path / "outside_secret"
    root_dir.mkdir()
    outside.mkdir()
    (outside / "passwords.txt").write_text("hunter2\n")
    (outside / "subdir").mkdir()
    (outside / "subdir" / "more.txt").write_text("nested\n")
    (root_dir / "ok.txt").write_text("fine\n")
    link = root_dir / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    mcp = build_server(Root(root_dir))
    blocks = _blocks(asyncio.run(mcp.call_tool("list_dir", {"path": ".", "depth": 3})))
    paths = [e["path"] for e in json.loads(blocks[0].text)["entries"]]

    assert "ok.txt" in paths  # in-root content still listed
    # Nothing reached through the escaping symlink may appear.
    assert not any("passwords.txt" in p for p in paths)
    assert not any("more.txt" in p for p in paths)
    assert not any(p.startswith("escape/") for p in paths)


def test_list_dir_does_not_descend_symlinked_dir_in_root(tmp_path):
    """A symlink to a directory *inside* the root is listed but not descended,
    preventing symlink cycles. The real subtree still recurses."""
    root_dir = tmp_path / "root"
    real = root_dir / "real"
    (real / "deep").mkdir(parents=True)
    (real / "deep" / "leaf.txt").write_text("x\n")
    link = root_dir / "alias"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    mcp = build_server(Root(root_dir))
    blocks = _blocks(asyncio.run(mcp.call_tool("list_dir", {"path": ".", "depth": 5})))
    paths = [e["path"] for e in json.loads(blocks[0].text)["entries"]]

    assert "real/deep/leaf.txt" in paths  # real subtree recurses
    # The in-root symlink is not expanded (no cycle/duplication through it).
    assert not any(p.startswith("alias/") for p in paths)


def test_read_file_text_paging(tmp_path):
    """read_file text paging: returns the requested window, reports total_lines, and
    signals end-of-file with next_offset=None."""
    f = tmp_path / "big.txt"
    f.write_text("".join(f"line{i}\n" for i in range(1, 1001)))  # 1000 lines
    mcp = build_server(Root(tmp_path))

    def read(**kw):
        blocks = _blocks(
            asyncio.run(mcp.call_tool("read_file", {"path": "big.txt", **kw}))
        )
        return json.loads(blocks[0].text)

    first = read(offset=1, limit=10)
    assert first["total_lines"] == 1000
    assert first["returned_lines"] == 10
    assert first["next_offset"] == 11
    assert first["content"].startswith("line1\n")

    tail = read(offset=995, limit=50)
    assert tail["returned_lines"] == 6  # lines 995..1000
    assert tail["next_offset"] is None  # exhausted
    assert tail["content"].endswith("line1000\n")

    past = read(offset=5000, limit=10)  # beyond EOF
    assert past["returned_lines"] == 0
    assert past["next_offset"] is None


def test_grep_basic_match_and_line_length_cap(tmp_path):
    """grep finds matches within the per-line scan cap and ignores content past it
    (the ReDoS line-length guard)."""
    from filebridge_mcp.config import GREP_LINE_MAX

    (tmp_path / "a.txt").write_text("alpha\nbeta needle gamma\n")
    # A single long line whose only match sits *beyond* the scan cap.
    (tmp_path / "long.txt").write_text("x" * (GREP_LINE_MAX + 50) + "FINDME\n")
    mcp = build_server(Root(tmp_path))

    def grep(**kw):
        blocks = _blocks(asyncio.run(mcp.call_tool("grep", kw)))
        return json.loads(blocks[0].text)

    hit = grep(pattern="needle")
    assert hit["count"] == 1
    assert hit["matches"][0]["line"] == 2
    assert hit["matches"][0]["path"] == "a.txt"

    # "FINDME" is past GREP_LINE_MAX, so the capped scan must not see it...
    assert grep(pattern="FINDME")["count"] == 0
    # ...but content within the cap on that same line is found.
    assert grep(pattern="x{10}")["count"] >= 1


def test_read_file_image_preserves_transparency(tmp_path):
    """A downscaled/re-encoded PNG must keep its alpha channel: it used to be
    flattened to RGB (convert('RGB')), turning transparent pixels opaque."""
    PILImage = pytest.importorskip("PIL.Image")
    import base64
    import io

    PILImage.new("RGBA", (128, 128), (255, 0, 0, 0)).save(
        tmp_path / "logo.png", format="PNG"
    )
    mcp = build_server(Root(tmp_path))
    blocks = _blocks(
        asyncio.run(
            mcp.call_tool("read_file", {"path": "logo.png", "max_dimension": 64})
        )
    )
    img = next(b for b in blocks if b.type == "image")
    out = PILImage.open(io.BytesIO(base64.b64decode(img.data)))
    assert out.mode == "RGBA"
    assert out.getpixel((0, 0))[3] == 0


def test_delete_refuses_sandbox_root(tmp_path):
    """delete('.', recursive=True) must not wipe the sandbox root itself."""
    (tmp_path / "keep.txt").write_text("data\n")
    mcp = build_server(Root(tmp_path), allow_delete=True)
    payload = json.loads(
        _blocks(asyncio.run(mcp.call_tool("delete", {"path": ".", "recursive": True})))[
            0
        ].text
    )
    assert "error" in payload
    assert (tmp_path / "keep.txt").exists()


def test_write_file_into_directory_errors_gracefully(tmp_path):
    """Writing onto an existing directory returns a JSON error, not a raw exception."""
    (tmp_path / "adir").mkdir()
    mcp = build_server(Root(tmp_path), allow_write=True)
    payload = json.loads(
        _blocks(
            asyncio.run(mcp.call_tool("write_file", {"path": "adir", "content": "x"}))
        )[0].text
    )
    assert "error" in payload


def test_make_dir_over_existing_file_errors_gracefully(tmp_path):
    """make_dir where a file already exists returns a JSON error, not FileExistsError."""
    (tmp_path / "afile").write_text("hi\n")
    mcp = build_server(Root(tmp_path), allow_write=True)
    payload = json.loads(
        _blocks(asyncio.run(mcp.call_tool("make_dir", {"path": "afile"})))[0].text
    )
    assert "error" in payload
    assert (tmp_path / "afile").is_file()


def test_move_into_existing_directory_reports_final_path(tmp_path):
    """Moving into an existing directory reports the real final path d/<name>."""
    (tmp_path / "f.txt").write_text("hi\n")
    (tmp_path / "dest").mkdir()
    mcp = build_server(Root(tmp_path), allow_delete=True)
    payload = json.loads(
        _blocks(asyncio.run(mcp.call_tool("move", {"src": "f.txt", "dst": "dest"})))[
            0
        ].text
    )
    assert payload["dst"] == "dest/f.txt"
    assert (tmp_path / "dest" / "f.txt").exists()


def test_pdf_text_offset_past_end_errors(tmp_path):
    """Reading PDF text past the last page returns an error, like render_page."""
    fitz = pytest.importorskip("fitz")
    doc = fitz.open()
    doc.new_page()
    doc.save(str(tmp_path / "doc.pdf"))
    doc.close()
    mcp = build_server(Root(tmp_path))
    payload = json.loads(
        _blocks(
            asyncio.run(mcp.call_tool("read_file", {"path": "doc.pdf", "offset": 5}))
        )[0].text
    )
    assert "error" in payload
    assert "out of range" in payload["error"]
