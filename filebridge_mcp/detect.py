"""File-type detection and the binary hexdump helper.

Detection (design decision D1) drives `read_file`'s type dispatch: magic bytes
first, then extension, then a UTF-8 sniff of the first few KB. Depends only on
`config` — no MCP SDK, so it is testable standalone.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from . import config


def looks_text(sample: bytes) -> bool:
    """Heuristic: is this byte sample decodable UTF-8 text (no NUL bytes)?"""
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # tolerate a partial multibyte char at the truncation boundary
        try:
            sample[:-3].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False


def detect_kind(p: Path) -> tuple[str, str]:
    """Return (kind, mime).

    kind in {text, image, pdf, office, archive, video, audio, binary}.
    Magic bytes are checked before extension so a mislabeled file is classified
    by content; ``PK\\x03\\x04`` disambiguates office (by extension) vs. archive.
    """
    ext = p.suffix.lower()
    if ext in config.VIDEO_EXTS:
        return "video", mimetypes.guess_type(p.name)[0] or "video/unknown"
    if ext in config.AUDIO_EXTS:
        return "audio", mimetypes.guess_type(p.name)[0] or "audio/unknown"
    if ext in config.OFFICE_EXTS:
        return "office", mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    try:
        with p.open("rb") as fh:
            head = fh.read(config.TEXT_SNIFF_BYTES)
    except OSError:
        head = b""
    for sig, kind, mime in config.MAGIC:
        if head.startswith(sig):
            return kind, mime
    if head.startswith(b"PK\x03\x04"):
        return ("office", "application/octet-stream") if ext in config.OFFICE_EXTS else ("archive", "application/zip")
    if ext in config.IMAGE_EXTS:
        return "image", mimetypes.guess_type(p.name)[0] or "image/unknown"
    if ext in config.ARCHIVE_EXTS:
        return "archive", "application/octet-stream"
    if looks_text(head):
        return "text", mimetypes.guess_type(p.name)[0] or "text/plain"
    return "binary", mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def hexdump(data: bytes) -> str:
    """Classic 16-byte-per-row hexdump with an ASCII gutter."""
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{i:08x}  {hexs:<47}  {ascii_}")
    return "\n".join(out)
