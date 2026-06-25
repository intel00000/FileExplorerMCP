"""Contact-sheet composition: tile several frames into one labeled image.

A single composite costs the vision encoder far fewer tokens than N separate
frames, at the price of per-tile resolution — the right trade for an at-a-glance
scan of a whole clip. Pillow-only (no MCP SDK), so it is unit-testable.
"""

from __future__ import annotations

import io
import math
from typing import Optional

from .. import deps


def available() -> bool:
    return deps.HAVE_PIL


def contact_sheet(
    tiles: list[tuple[str, bytes]],
    cols: int,
    max_dimension: int,
    fmt: str = "jpeg",
    quality: int = 85,
    label: bool = True,
    bg: tuple[int, int, int] = (16, 16, 16),
) -> bytes:
    """Compose `tiles` (each a (caption, encoded-image-bytes)) into a grid.

    Returns the encoded sheet bytes in `fmt` ('jpeg' or 'png'). Raises if Pillow
    is missing or no tile decodes. The whole sheet is bounded to `max_dimension`
    on its longest edge.
    """
    if not deps.HAVE_PIL:
        raise RuntimeError("Contact sheet needs Pillow. Run: pip install pillow")
    from PIL import ImageDraw, ImageFont

    PILImage = deps.PILImage
    imgs: list[tuple[str, "PILImage.Image"]] = []
    for caption, data in tiles:
        try:
            im = PILImage.open(io.BytesIO(data)).convert("RGB")
            imgs.append((caption, im))
        except Exception:
            continue  # skip a frame that won't decode; the sheet still renders
    if not imgs:
        raise RuntimeError("No frames could be decoded for the contact sheet")

    n = len(imgs)
    cols = max(1, min(cols, n))
    rows = math.ceil(n / cols)
    cell_w = max(im.width for _, im in imgs)
    cell_h = max(im.height for _, im in imgs)

    sheet = PILImage.new("RGB", (cols * cell_w, rows * cell_h), bg)
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.load_default()
    except Exception:  # pragma: no cover - font always present in practice
        font = None

    for i, (caption, im) in enumerate(imgs):
        r, c = divmod(i, cols)
        x, y = c * cell_w, r * cell_h
        # center within the cell (frames share a size, so offset is usually 0)
        sheet.paste(im, (x + (cell_w - im.width) // 2, y + (cell_h - im.height) // 2))
        if label and caption:
            tw, th = _text_size(draw, caption, font)
            draw.rectangle([x, y, x + tw + 6, y + th + 4], fill=(0, 0, 0))
            draw.text((x + 3, y + 2), caption, fill=(255, 255, 0), font=font)

    if max_dimension and max(sheet.size) > max_dimension:
        sheet.thumbnail((max_dimension, max_dimension))

    out = io.BytesIO()
    fmt = "jpeg" if fmt.lower() in ("jpg", "jpeg") else "png"
    if fmt == "jpeg":
        sheet.save(out, format="JPEG", quality=max(1, min(100, quality)))
    else:
        sheet.save(out, format="PNG")
    return out.getvalue()


def _text_size(draw, text: str, font) -> tuple[int, int]:
    """Pillow-version-agnostic text measurement."""
    if hasattr(draw, "textbbox"):
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
        return r - l, b - t
    return draw.textsize(text, font=font)  # pragma: no cover - very old Pillow
