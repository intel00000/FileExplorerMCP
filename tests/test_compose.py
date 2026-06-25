"""Contact-sheet composition + frame-encoding helper tests (SDK-free, Pillow-only)."""

from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL")

from PIL import Image as PILImage  # noqa: E402

from filebridge_mcp.media import compose                      # noqa: E402
from filebridge_mcp.media.video import _jpeg_qscale, norm_format  # noqa: E402


def _png(color, size=(40, 30)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def test_norm_format():
    assert norm_format("jpg") == "jpeg"
    assert norm_format("JPEG") == "jpeg"
    assert norm_format("png") == "png"
    assert norm_format("") == "png"


def test_jpeg_qscale_monotonic_and_bounded():
    # higher quality -> lower (better) q:v; always within ffmpeg's 2..31
    assert _jpeg_qscale(100) < _jpeg_qscale(1)
    assert _jpeg_qscale(2) == 2 or _jpeg_qscale(100) == 2
    for q in (1, 50, 100, -5, 999):
        assert 2 <= _jpeg_qscale(q) <= 31


def test_contact_sheet_grid_dimensions():
    tiles = [(f"{i}s", _png((40 * i, 0, 0))) for i in range(5)]
    out = compose.contact_sheet(tiles, cols=2, max_dimension=500, fmt="png")
    im = PILImage.open(io.BytesIO(out))
    assert im.format == "PNG"
    # 5 tiles, 2 cols -> 3 rows; cell 40x30 -> 80x90, under max_dimension (no shrink)
    assert im.size == (80, 90)


def test_contact_sheet_jpeg_output():
    out = compose.contact_sheet([("a", _png((1, 2, 3)))], cols=1, max_dimension=200, fmt="jpeg", quality=60)
    assert PILImage.open(io.BytesIO(out)).format == "JPEG"


def test_contact_sheet_caps_max_dimension():
    tiles = [("x", _png((9, 9, 9), size=(400, 300)))]
    out = compose.contact_sheet(tiles, cols=1, max_dimension=100, fmt="png")
    assert max(PILImage.open(io.BytesIO(out)).size) <= 100


def test_contact_sheet_skips_undecodable_tiles():
    tiles = [("good", _png((0, 128, 0))), ("bad", b"not an image")]
    out = compose.contact_sheet(tiles, cols=2, max_dimension=500, fmt="png")
    # one good tile survives, one column
    assert PILImage.open(io.BytesIO(out)).size == (40, 30)


def test_contact_sheet_all_bad_raises():
    with pytest.raises(RuntimeError):
        compose.contact_sheet([("bad", b"xxx")], cols=1, max_dimension=100, fmt="png")
