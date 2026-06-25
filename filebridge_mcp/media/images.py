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


def image_content(data: bytes, fmt: str = "png", meta: Optional[dict] = None) -> ImageContent:
    """Build an MCP ImageContent from raw bytes, stamping `_meta` for identity.

    Returning a typed ImageContent (rather than the bare FastMCP `Image`) lets us
    attach `_meta` to each block — e.g. {"path", "timestamp_sec", "frame_index"} —
    so the host can isolate AND identify an image without relying on block order.
    The `_meta` survives the FastMCP tool-result round-trip unchanged.
    """
    mime = "image/jpeg" if fmt.lower() in ("jpg", "jpeg") else "image/png"
    b64 = base64.b64encode(data).decode("ascii")
    return ImageContent(type="image", data=b64, mimeType=mime, _meta=meta or None)


def downscaled_image(p: Path, max_dim: Optional[int]) -> Image:
    """Return a FastMCP `Image` for an image file, optionally downscaled.

    `thumbnail` only ever shrinks, so a smaller-than-`max_dim` image is returned
    near-unchanged (re-encoded to PNG). Without Pillow, the file is handed off
    by path and the format is inferred from its extension.
    """
    if max_dim and deps.HAVE_PIL:
        with deps.PILImage.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((max_dim, max_dim))
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return Image(data=buf.getvalue(), format="png")
    return Image(path=str(p))  # format inferred from extension
