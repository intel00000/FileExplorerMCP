"""Still-image projection: return an MCP `Image` for the vision channel.

Imports the MCP SDK's `Image` type, so (unlike `media.video`) this module needs
the SDK present. Downscaling is Pillow-gated; without Pillow the image is returned
at native resolution.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import Image

from .. import deps


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
