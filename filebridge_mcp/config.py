"""Static configuration for filebridge_mcp.

Pure constants and lookup tables — no imports of the MCP SDK or any optional
dependency, so this module (and everything that depends only on it) is importable
and testable in a minimal environment.
"""

from __future__ import annotations

# --- Bounds -----------------------------------------------------------------
DEFAULT_LINES = 400          # default text window (lines) when caller omits limit
MAX_LINES = 5000             # hard ceiling per read_file call
DEFAULT_FRAMES = 8           # default frames sampled across a video slice
MAX_FRAMES = 64
DEFAULT_MAX_DIM = 1024       # cap longest image/frame edge (VRAM/encode cost lever)
HEXDUMP_BYTES = 256          # bytes shown for unknown-binary reads
TEXT_SNIFF_BYTES = 4096      # bytes sampled to decide "is this text?"
GREP_MAX_FILE = 5_000_000    # skip files larger than this when grepping

# --- Extension sets ---------------------------------------------------------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}

# --- Magic-byte signatures (checked before extension) -----------------------
MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF8", "image", "image/gif"),
    (b"BM", "image", "image/bmp"),
    (b"%PDF", "pdf", "application/pdf"),
]

# --- MCP tool annotation presets --------------------------------------------
# RO          = read-only, non-destructive, idempotent
# WRITE       = mutating, non-destructive, non-idempotent
# DESTRUCTIVE = mutating, destructive, non-idempotent
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
