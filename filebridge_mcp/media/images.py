"""Still-image projection: return an MCP `Image` for the vision channel.

Imports the MCP SDK's `Image` type, so (unlike `media.video`) this module needs
the SDK present. Downscaling is Pillow-gated; without Pillow the image is returned
at native resolution.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import Image
from mcp.types import ImageContent

from .. import deps
from .video import norm_format


def image_content(
    data: bytes, fmt: str = "png", meta: Optional[dict] = None
) -> ImageContent:
    """Build an MCP ImageContent from raw bytes, stamping `_meta` for identity.

    Returning a typed ImageContent (rather than the bare FastMCP `Image`) lets us
    attach `_meta` to each block — e.g. {"path", "timestamp_sec", "frame_index"} —
    so the host can isolate AND identify an image without relying on block order.
    The `_meta` survives the FastMCP tool-result round-trip unchanged.
    """
    mime = "image/jpeg" if fmt.lower() in ("jpg", "jpeg") else "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return ImageContent(type="image", data=b64, mimeType=mime, _meta=meta or None)


def downscaled_image(
    p: Path,
    max_dim: Optional[int],
    fmt: Optional[str] = None,
    quality: Optional[int] = None,
) -> Image:
    """Return a FastMCP `Image` for an image file, optionally resized and/or re-encoded.

    Re-encodes (via Pillow) when a `max_dim` cap is given OR a `fmt` is requested:
    `thumbnail` only ever shrinks (aspect preserved); `fmt` picks png (lossless) or
    jpeg (`quality` 1..100, default 85). When neither is set — or Pillow is absent —
    the file is handed off by path at native resolution/format.
    """
    if (max_dim or fmt) and deps.HAVE_PIL:
        from PIL import ImageOps

        with deps.PILImage.open(p) as im:
            im = ImageOps.exif_transpose(im)  # honor camera orientation (no-op if none)
            out = norm_format(fmt)  # None -> "png"
            if out == "jpeg":
                im = im.convert("RGB")  # JPEG has no alpha channel
            elif im.mode not in ("RGB", "RGBA", "L", "LA"):
                # PNG: normalize exotic modes (P/CMYK/I/…) but keep any transparency.
                im = im.convert("RGBA")
            if max_dim:
                im.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            if out == "jpeg":
                im.save(buf, format="JPEG", quality=quality or 85)
            else:
                im.save(buf, format="PNG")
            return Image(data=buf.getvalue(), format=out)
    return Image(path=str(p))  # native resolution + format inferred from extension
