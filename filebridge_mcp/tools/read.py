"""Read tools: read_file (type-dispatched) and read_bytes.

`read_file` is the design's centerpiece (D1): it never returns raw bytes, it
projects each file into the text channel or the vision channel based on detected
kind. The text branch streams a bounded window rather than slurping the whole
file, so a multi-GB log costs `limit` lines of memory, not the file's size.
"""

from __future__ import annotations

import json
from typing import Annotated, Optional

from pydantic import Field

from .. import deps
from ..config import (
    DEFAULT_LINES,
    HEXDUMP_BYTES,
    MAX_LINES,
    PDF_RENDER_DIM,
    RO,
    resolve_dim,
)
from ..detect import detect_kind, hexdump
from ..media.images import downscaled_image, image_content
from ..media.video import norm_format
from ..sandbox import Root

_FMT = Field(
    description="Image encoding for image/PDF-render output: 'png' (lossless) or "
    "'jpeg' (smaller). Omit to use the server default (native for images).",
    pattern="^(png|jpe?g)$",
)
_QUALITY = Field(
    description="JPEG quality 1..100 (higher=better); ignored for png.", ge=1, le=100
)


def register(
    mcp,
    root: Root,
    *,
    max_image_dim: Optional[int] = None,
    image_format: Optional[str] = None,
    image_quality: Optional[int] = None,
) -> None:
    @mcp.tool(name="read_file", annotations={"title": "Read file content", **RO})
    def read_file(
        path: Annotated[str, Field(description="File relative to root")],
        offset: Annotated[
            int,
            Field(
                description="Start of slice. Lines for text, page number for PDF (1-indexed).",
                ge=1,
            ),
        ] = 1,
        limit: Annotated[
            int,
            Field(
                description="Slice size: number of lines (text) or pages (PDF) to return.",
                ge=1,
                le=MAX_LINES,
            ),
        ] = DEFAULT_LINES,
        max_dimension: Annotated[
            Optional[int],
            Field(
                description="Cap the longest edge of image/PDF-render output (px). Omit "
                "for no cap (native size). The effective cap is the smaller of this and "
                "any server-set --max-image-dimension.",
                ge=64,
                le=8192,
            ),
        ] = None,
        render_page: Annotated[
            bool,
            Field(
                description="For PDFs: return the page at `offset` as an image instead of text."
            ),
        ] = False,
        format: Annotated[Optional[str], _FMT] = None,
        quality: Annotated[Optional[int], _QUALITY] = None,
    ):
        """Read one file, projecting it into text or image based on detected type.

        Dispatch by kind:
          text    -> a line slice [offset, offset+limit); JSON wraps the content with
                     total_lines and next_offset so you can page large files.
          image   -> the image itself (downscaled to max_dimension) for the vision model.
          pdf     -> text of pages [offset, offset+limit); or, with render_page=true,
                     the single page at `offset` rasterized to an image.
          video / audio -> metadata only; use video_frame/video_frames to see footage.
          archive -> a note to inspect entries (extension point).
          office  -> a note that extraction isn't built into this skeleton.
          binary  -> stat + a hexdump of the first bytes (never the raw blob).

        Returns either a JSON string or an Image.
        """
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        kind, mime = detect_kind(p)

        if kind == "text":
            start = offset - 1
            window: list[str] = []
            total_lines = 0
            # Stream: hold only the window in memory, count the rest.
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if start <= i < start + limit:
                        window.append(line)
                    total_lines = i + 1
            nxt = offset + len(window)
            return json.dumps(
                {
                    "path": root.rel(p),
                    "kind": "text",
                    "total_lines": total_lines,
                    "offset": offset,
                    "returned_lines": len(window),
                    "next_offset": nxt if nxt <= total_lines else None,
                    "content": "".join(window),
                },
                indent=2,
            )

        dim = resolve_dim(max_dimension, max_image_dim)
        # Per-call format/quality win; else the server default; else native (image)
        # or png (PDF render). `img_fmt` None means "leave images in native format".
        img_fmt = format or image_format
        img_q = quality or image_quality

        if kind == "image":
            try:
                # Convert to ImageContent (preserves mime for png/jpeg/gif/webp/bmp/tiff)
                # and stamp _meta so the host can identify the image, like the video tools.
                ic = downscaled_image(p, dim, img_fmt, img_q).to_image_content()
                ic.meta = {"path": root.rel(p), "kind": "image"}
                return ic
            except (
                Exception
            ) as e:  # corrupt/truncated/unsupported image — report, don't crash
                return json.dumps({
                    "error": root.scrub(f"Could not open image '{root.rel(p)}': {e}")
                })

        if kind == "pdf":
            if not deps.HAVE_FITZ:
                return json.dumps({"error": deps.PDF_MISSING})
            try:
                doc = deps.fitz.open(p)
                n = doc.page_count
                if render_page:
                    if offset > n:
                        return json.dumps({
                            "error": f"PDF has {n} pages; offset {offset} out of range"
                        })
                    page = doc.load_page(offset - 1)
                    # A vector page has no native pixel size, so an uncapped (None)
                    # request falls back to a sane render resolution.
                    render_dim = dim if dim is not None else PDF_RENDER_DIM
                    zoom = render_dim / max(page.rect.width, page.rect.height)
                    pix = page.get_pixmap(matrix=deps.fitz.Matrix(zoom, zoom))
                    page_fmt = norm_format(img_fmt)  # None -> "png"
                    blob = (
                        pix.tobytes("jpg", jpg_quality=img_q or 85)
                        if page_fmt == "jpeg"
                        else pix.tobytes("png")
                    )
                    return image_content(
                        blob,
                        page_fmt,
                        {"path": root.rel(p), "kind": "pdf_page", "page": offset},
                    )
                texts = []
                for i in range(offset - 1, min(offset - 1 + limit, n)):
                    texts.append(f"--- page {i + 1} ---\n{doc.load_page(i).get_text()}")
                nxt = offset + limit
                return json.dumps(
                    {
                        "path": root.rel(p),
                        "kind": "pdf",
                        "total_pages": n,
                        "offset": offset,
                        "next_offset": nxt if nxt <= n else None,
                        "content": "\n".join(texts),
                    },
                    indent=2,
                )
            except Exception as e:  # damaged/encrypted PDF — report, don't crash
                return json.dumps({
                    "error": root.scrub(f"Could not read PDF '{root.rel(p)}': {e}")
                })

        if kind == "office":
            return json.dumps({
                "path": root.rel(p),
                "kind": "office",
                "mime": mime,
                "note": "Office extraction is an extension point (use python-docx/"
                "python-pptx/openpyxl). Not implemented in this skeleton.",
            })
        if kind == "archive":
            return json.dumps({
                "path": root.rel(p),
                "kind": "archive",
                "note": "Archive listing is an extension point. Inspect entries before extracting.",
            })
        if kind in ("video", "audio"):
            return json.dumps({
                "path": root.rel(p),
                "kind": kind,
                "mime": mime,
                "note": "Use stat for metadata and video_frame/video_frames to view footage; "
                "raw media bytes are not returned.",
            })

        with p.open("rb") as fh:
            data = fh.read(HEXDUMP_BYTES)
        return json.dumps(
            {
                "path": root.rel(p),
                "kind": "binary",
                "mime": mime,
                "size": p.stat().st_size,
                "shown_bytes": len(data),
                "hexdump": hexdump(data),
            },
            indent=2,
        )

    @mcp.tool(name="read_bytes", annotations={"title": "Read a byte slice", **RO})
    def read_bytes(
        path: Annotated[str, Field(description="File relative to root")],
        offset: Annotated[int, Field(description="Byte offset to start at", ge=0)] = 0,
        length: Annotated[
            int, Field(description="Number of bytes to read", ge=1, le=4096)
        ] = HEXDUMP_BYTES,
    ) -> str:
        """Hexdump an arbitrary byte slice of any file — for inspecting binary formats.

        Returns JSON: {"path","offset","length","total_size","hexdump"}.
        """
        p = root.resolve(path)
        if not p.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(length)
        return json.dumps(
            {
                "path": root.rel(p),
                "offset": offset,
                "length": len(data),
                "total_size": size,
                "hexdump": hexdump(data),
            },
            indent=2,
        )
