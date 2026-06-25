"""Optional-dependency detection and actionable "missing dependency" messages.

Design decision D6 — graceful degradation. The core (explore, text read, binary
hexdump, write, search) is standard-library only. Pillow, PyMuPDF, and ffmpeg are
optional; when absent, the affected tool returns one of these messages instead of
failing to import.
"""

from __future__ import annotations

import shutil
from typing import Optional

# Pillow — image downscaling.
try:
    from PIL import Image as PILImage  # noqa: F401  (re-exported for media.images)
    HAVE_PIL = True
except ImportError:  # pragma: no cover - environment dependent
    PILImage = None  # type: ignore[assignment]
    HAVE_PIL = False

# PyMuPDF — PDF text extraction and page rasterization.
try:
    import fitz  # noqa: F401  (re-exported for tools.read)  PyMuPDF
    HAVE_FITZ = True
except ImportError:  # pragma: no cover - environment dependent
    fitz = None  # type: ignore[assignment]
    HAVE_FITZ = False


def have(binary: str) -> bool:
    """True if `binary` is resolvable on PATH."""
    return shutil.which(binary) is not None


def require_ffmpeg() -> Optional[str]:
    """Return an actionable error string if ffmpeg/ffprobe are missing, else None."""
    missing = [b for b in ("ffmpeg", "ffprobe") if not have(b)]
    if missing:
        return (
            f"Error: {', '.join(missing)} not found on PATH. Install FFmpeg "
            f"(https://ffmpeg.org/download.html) and restart the server."
        )
    return None


PDF_MISSING = "PDF support needs PyMuPDF. Run: pip install pymupdf"
