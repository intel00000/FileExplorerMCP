#!/usr/bin/env python3
"""
filebridge_mcp — a standalone MCP server that exposes a single folder (Windows or
Linux) to a VLM as an FTP-like surface: explore, read, write, search, plus
timestamp-sliced video frame extraction.

The core idea: a VLM can only consume *text* and *images*. This server never
streams opaque bytes; `read_file` detects the file type and projects it into one
of those two channels (text content, or an Image that reaches the vision encoder).

Run (stdio, the usual local-MCP transport):
    pip install "mcp[cli]"            # required
    pip install pillow pymupdf        # optional: image downscale + PDF text/render
    # ffmpeg + ffprobe on PATH        # optional: video tools
    python filebridge_mcp.py --root /path/to/folder

Configure in any MCP host (llama-server's MCP client, Claude Desktop, Continue):
    "filebridge": {"command": "python",
                   "args": ["filebridge_mcp.py", "--root", "/path/to/folder"]}

Remote/multi-client instead of stdio:
    python filebridge_mcp.py --root /path/to/folder --http --port 8000

SECURITY: every path is resolved and confined under --root; traversal and symlink
escapes are rejected. Still run it against a folder you're willing to expose.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Annotated, Optional

from pydantic import Field
from mcp.server.fastmcp import FastMCP, Image

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_LINES = 400          # default text window (lines) when caller omits limit
MAX_LINES = 5000             # hard ceiling per read_file call
DEFAULT_FRAMES = 8           # default frames sampled across a video slice
MAX_FRAMES = 64
DEFAULT_MAX_DIM = 1024       # cap longest image/frame edge (VRAM/encode cost lever)
HEXDUMP_BYTES = 256          # bytes shown for unknown-binary reads
TEXT_SNIFF_BYTES = 4096      # bytes sampled to decide "is this text?"
GREP_MAX_FILE = 5_000_000    # skip files larger than this when grepping

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".opus", ".wma"}
OFFICE_EXTS = {".docx", ".pptx", ".xlsx"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar"}

_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", "image", "image/png"),
    (b"\xff\xd8\xff", "image", "image/jpeg"),
    (b"GIF8", "image", "image/gif"),
    (b"BM", "image", "image/bmp"),
    (b"%PDF", "pdf", "application/pdf"),
]

# Optional dependencies — the server runs without them; affected tools degrade
# with an actionable message rather than crashing.
try:
    from PIL import Image as PILImage  # noqa: N812
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False

try:
    import fitz  # PyMuPDF
    _HAVE_FITZ = True
except ImportError:
    _HAVE_FITZ = False

mcp = FastMCP(
    "filebridge_mcp",
    instructions=(
        "Sandboxed access to one folder. Orient with list_dir/stat/glob/grep "
        "before reading. read_file auto-detects type: text returns a line slice "
        "(use offset/limit to page), images and PDF-page renders return as images "
        "the model can see, video/audio return metadata only — use video_frame / "
        "video_frames to actually see footage. All paths are relative to the root."
    ),
)

RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}


# ---------------------------------------------------------------------------
# Sandbox + helpers
# ---------------------------------------------------------------------------
def _root() -> Path:
    return Path(os.environ.get("MCP_ROOT", ".")).expanduser().resolve()


def _resolve(rel: str) -> Path:
    """Resolve a caller-supplied relative path, confined under the root.

    Leading separators are stripped (input is always treated as relative), the
    path is fully resolved (following symlinks), then containment is re-checked —
    so a symlink pointing outside the root is rejected too.
    """
    cleaned = rel.replace("\\", "/").lstrip("/")
    target = (_root() / cleaned).resolve()
    root = _root()
    if target != root and root not in target.parents:
        raise ValueError(
            f"Path '{rel}' escapes the sandbox root. Use a path inside the root; "
            f"'..' and absolute paths outside the root are not allowed."
        )
    return target


def _rel(p: Path) -> str:
    """Present POSIX-style relative paths so model behavior is OS-independent."""
    try:
        return p.relative_to(_root()).as_posix() or "."
    except ValueError:
        return p.as_posix()


def _looks_text(sample: bytes) -> bool:
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


def _detect_kind(p: Path) -> tuple[str, str]:
    """Return (kind, mime). kind in {text,image,pdf,office,archive,video,audio,binary}.

    Detection is by magic bytes first, then extension, then a UTF-8 sniff.
    """
    ext = p.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video", mimetypes.guess_type(p.name)[0] or "video/unknown"
    if ext in AUDIO_EXTS:
        return "audio", mimetypes.guess_type(p.name)[0] or "audio/unknown"
    if ext in OFFICE_EXTS:
        return "office", mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    try:
        head = p.open("rb").read(TEXT_SNIFF_BYTES)
    except OSError:
        head = b""
    for sig, kind, mime in _MAGIC:
        if head.startswith(sig):
            return kind, mime
    if head.startswith(b"PK\x03\x04"):
        return ("office", "application/octet-stream") if ext in OFFICE_EXTS else ("archive", "application/zip")
    if ext in IMAGE_EXTS:
        return "image", mimetypes.guess_type(p.name)[0] or "image/unknown"
    if ext in ARCHIVE_EXTS:
        return "archive", "application/octet-stream"
    if _looks_text(head):
        return "text", mimetypes.guess_type(p.name)[0] or "text/plain"
    return "binary", mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def _hexdump(data: bytes) -> str:
    out = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexs = " ".join(f"{b:02x}" for b in chunk)
        ascii_ = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        out.append(f"{i:08x}  {hexs:<47}  {ascii_}")
    return "\n".join(out)


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _downscaled_image(p: Path, max_dim: Optional[int]) -> Image:
    """Return a FastMCP Image for an image file, optionally downscaled."""
    if max_dim and _HAVE_PIL:
        with PILImage.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((max_dim, max_dim))
            import io
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return Image(data=buf.getvalue(), format="png")
    return Image(path=str(p))  # format inferred from extension


# ---- video ----------------------------------------------------------------
def _ffprobe(p: Path) -> dict:
    cmd = ["ffprobe", "-loglevel", "error", "-show_format", "-show_streams", "-of", "json", str(p)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip()[:300] or "ffprobe failed")
    return json.loads(res.stdout)


def _duration(p: Path) -> float:
    info = _ffprobe(p)
    dur = info.get("format", {}).get("duration")
    if dur is None:
        for s in info.get("streams", []):
            if s.get("duration"):
                dur = s["duration"]
                break
    return float(dur) if dur else 0.0


def _extract_frame(p: Path, t: float, max_dim: Optional[int]) -> bytes:
    """Grab one PNG frame at time t (seconds). Fast keyframe seek (-ss before -i)."""
    cmd = ["ffmpeg", "-loglevel", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(p), "-frames:v", "1"]
    if max_dim:
        cmd += ["-vf", f"scale='min({int(max_dim)},iw)':-2"]
    cmd += ["-f", "image2", "-c:v", "png", "-"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError(res.stderr.decode(errors="replace").strip()[:300] or "ffmpeg produced no frame")
    return res.stdout


def _require_ffmpeg() -> Optional[str]:
    missing = [b for b in ("ffmpeg", "ffprobe") if not _have(b)]
    if missing:
        return (f"Error: {', '.join(missing)} not found on PATH. Install FFmpeg "
                f"(https://ffmpeg.org/download.html) and restart the server.")
    return None


# ---------------------------------------------------------------------------
# Explore
# ---------------------------------------------------------------------------
@mcp.tool(name="list_dir", annotations={"title": "List directory", **RO})
def list_dir(
    path: Annotated[str, Field(description="Folder relative to root, e.g. '.' or 'movies/2024'")] = ".",
    depth: Annotated[int, Field(description="Recursion depth (1 = immediate children)", ge=1, le=5)] = 1,
    max_entries: Annotated[int, Field(description="Cap on entries returned", ge=1, le=2000)] = 500,
) -> str:
    """List a directory as a tree, for orientation before reading files.

    Returns JSON: {"path", "entries":[{"path","type","size","mtime"}], "truncated"}.
    type is "dir" or a detected file kind (text/image/pdf/video/audio/...).
    """
    base = _resolve(path)
    if not base.is_dir():
        return json.dumps({"error": f"Not a directory: {path}"})
    entries, truncated = [], False
    stack = [(base, 0)]
    while stack:
        cur, d = stack.pop(0)
        try:
            children = sorted(cur.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except OSError:
            continue
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                break
            is_dir = child.is_dir()
            try:
                st = child.stat()
                size, mtime = st.st_size, int(st.st_mtime)
            except OSError:
                size, mtime = 0, 0
            entries.append({
                "path": _rel(child),
                "type": "dir" if is_dir else _detect_kind(child)[0],
                "size": None if is_dir else size,
                "mtime": mtime,
            })
            if is_dir and d + 1 < depth:
                stack.append((child, d + 1))
        if truncated:
            break
    return json.dumps({"path": _rel(base), "entries": entries, "truncated": truncated}, indent=2)


@mcp.tool(name="stat", annotations={"title": "Stat a path", **RO})
def stat(path: Annotated[str, Field(description="File or folder relative to root")]) -> str:
    """Cheap metadata for one path — call this before read_file to size up a file.

    Returns JSON: {"path","exists","is_dir","kind","mime","size","mtime"}.
    For video/audio, also includes duration_sec / width / height when probable.
    """
    p = _resolve(path)
    if not p.exists():
        return json.dumps({"path": path, "exists": False})
    st = p.stat()
    out = {"path": _rel(p), "exists": True, "is_dir": p.is_dir(),
           "size": st.st_size, "mtime": int(st.st_mtime)}
    if p.is_dir():
        out["kind"] = "dir"
        return json.dumps(out, indent=2)
    kind, mime = _detect_kind(p)
    out["kind"], out["mime"] = kind, mime
    if kind in ("video", "audio") and not _require_ffmpeg():
        try:
            info = _ffprobe(p)
            out["duration_sec"] = round(_duration(p), 3)
            for s in info.get("streams", []):
                if s.get("codec_type") == "video":
                    out["width"], out["height"] = s.get("width"), s.get("height")
                    out["codec"] = s.get("codec_name")
                    break
        except RuntimeError:
            pass
    return json.dumps(out, indent=2)


# ---------------------------------------------------------------------------
# Read (type-dispatched, sliced)
# ---------------------------------------------------------------------------
@mcp.tool(name="read_file", annotations={"title": "Read file content", **RO})
def read_file(
    path: Annotated[str, Field(description="File relative to root")],
    offset: Annotated[int, Field(description="Start of slice. Lines for text, page number for PDF (1-indexed).", ge=1)] = 1,
    limit: Annotated[int, Field(description="Slice size: number of lines (text) or pages (PDF) to return.", ge=1, le=MAX_LINES)] = DEFAULT_LINES,
    max_dimension: Annotated[int, Field(description="Cap longest edge for image/PDF-render output (px).", ge=64, le=4096)] = DEFAULT_MAX_DIM,
    render_page: Annotated[bool, Field(description="For PDFs: return the page at `offset` as an image instead of text.")] = False,
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
    p = _resolve(path)
    if not p.is_file():
        return json.dumps({"error": f"Not a file: {path}"})
    kind, mime = _detect_kind(p)

    if kind == "text":
        with p.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        start = offset - 1
        window = lines[start:start + limit]
        nxt = offset + len(window)
        return json.dumps({
            "path": _rel(p), "kind": "text", "total_lines": len(lines),
            "offset": offset, "returned_lines": len(window),
            "next_offset": nxt if nxt <= len(lines) else None,
            "content": "".join(window),
        }, indent=2)

    if kind == "image":
        return _downscaled_image(p, max_dimension)

    if kind == "pdf":
        if not _HAVE_FITZ:
            return json.dumps({"error": "PDF support needs PyMuPDF. Run: pip install pymupdf"})
        doc = fitz.open(p)
        n = doc.page_count
        if render_page:
            if offset > n:
                return json.dumps({"error": f"PDF has {n} pages; offset {offset} out of range"})
            page = doc.load_page(offset - 1)
            zoom = max_dimension / max(page.rect.width, page.rect.height)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            return Image(data=pix.tobytes("png"), format="png")
        texts = []
        for i in range(offset - 1, min(offset - 1 + limit, n)):
            texts.append(f"--- page {i + 1} ---\n{doc.load_page(i).get_text()}")
        nxt = offset + limit
        return json.dumps({
            "path": _rel(p), "kind": "pdf", "total_pages": n, "offset": offset,
            "next_offset": nxt if nxt <= n else None, "content": "\n".join(texts),
        }, indent=2)

    if kind == "office":
        return json.dumps({"path": _rel(p), "kind": "office", "mime": mime,
                           "note": "Office extraction is an extension point (use python-docx/"
                                   "python-pptx/openpyxl). Not implemented in this skeleton."})
    if kind == "archive":
        return json.dumps({"path": _rel(p), "kind": "archive",
                           "note": "Archive listing is an extension point. Inspect entries before extracting."})
    if kind in ("video", "audio"):
        return json.dumps({"path": _rel(p), "kind": kind, "mime": mime,
                           "note": "Use stat for metadata and video_frame/video_frames to view footage; "
                                   "raw media bytes are not returned."})

    data = p.open("rb").read(HEXDUMP_BYTES)
    return json.dumps({"path": _rel(p), "kind": "binary", "mime": mime,
                       "size": p.stat().st_size, "shown_bytes": len(data),
                       "hexdump": _hexdump(data)}, indent=2)


@mcp.tool(name="read_bytes", annotations={"title": "Read a byte slice", **RO})
def read_bytes(
    path: Annotated[str, Field(description="File relative to root")],
    offset: Annotated[int, Field(description="Byte offset to start at", ge=0)] = 0,
    length: Annotated[int, Field(description="Number of bytes to read", ge=1, le=4096)] = HEXDUMP_BYTES,
) -> str:
    """Hexdump an arbitrary byte slice of any file — for inspecting binary formats.

    Returns JSON: {"path","offset","length","total_size","hexdump"}.
    """
    p = _resolve(path)
    if not p.is_file():
        return json.dumps({"error": f"Not a file: {path}"})
    size = p.stat().st_size
    with p.open("rb") as f:
        f.seek(offset)
        data = f.read(length)
    return json.dumps({"path": _rel(p), "offset": offset, "length": len(data),
                       "total_size": size, "hexdump": _hexdump(data)}, indent=2)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
@mcp.tool(name="glob", annotations={"title": "Glob for paths", **RO})
def glob(
    pattern: Annotated[str, Field(description="Glob relative to root, e.g. '**/*.srt' or 'movies/*.mp4'")],
    max_results: Annotated[int, Field(description="Cap on paths returned", ge=1, le=2000)] = 500,
) -> str:
    """Find paths by glob pattern (supports ** for recursion).

    Returns JSON: {"pattern","count","truncated","paths":[...]}.
    """
    root = _root()
    paths, truncated = [], False
    for m in root.glob(pattern):
        if len(paths) >= max_results:
            truncated = True
            break
        paths.append(_rel(m))
    return json.dumps({"pattern": pattern, "count": len(paths),
                       "truncated": truncated, "paths": paths}, indent=2)


@mcp.tool(name="grep", annotations={"title": "Grep file contents", **RO})
def grep(
    pattern: Annotated[str, Field(description="Python regex to search for")],
    path_glob: Annotated[str, Field(description="Restrict search to files matching this glob")] = "**/*",
    max_results: Annotated[int, Field(description="Cap on matching lines returned", ge=1, le=1000)] = 200,
    ignore_case: Annotated[bool, Field(description="Case-insensitive match")] = False,
) -> str:
    """Search text-file contents for a regex; pairs with read_file's line offsets.

    Returns JSON: {"pattern","count","truncated","matches":[{"path","line","text"}]}.
    Binary files and files over ~5 MB are skipped.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return json.dumps({"error": f"Invalid regex: {e}"})
    root = _root()
    matches, truncated = [], False
    for f in root.glob(path_glob):
        if not f.is_file() or f.stat().st_size > GREP_MAX_FILE:
            continue
        if _detect_kind(f)[0] not in ("text", "pdf"):
            continue
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if rx.search(line):
                        matches.append({"path": _rel(f), "line": n, "text": line.rstrip("\n")[:400]})
                        if len(matches) >= max_results:
                            truncated = True
                            break
        except OSError:
            continue
        if truncated:
            break
    return json.dumps({"pattern": pattern, "count": len(matches),
                       "truncated": truncated, "matches": matches}, indent=2)


# ---------------------------------------------------------------------------
# Write / mutate
# ---------------------------------------------------------------------------
@mcp.tool(name="write_file", annotations={"title": "Write a text file", **WRITE})
def write_file(
    path: Annotated[str, Field(description="Destination file relative to root")],
    content: Annotated[str, Field(description="Text content to write")],
    mode: Annotated[str, Field(description="'overwrite' (default) or 'append'", pattern="^(overwrite|append)$")] = "overwrite",
) -> str:
    """Write or append UTF-8 text to a file (parent dirs are created as needed).

    Binary writes are an extension point (accept base64 + decode). Returns JSON
    {"path","mode","bytes_written"}.
    """
    p = _resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if mode == "append" else "w", encoding="utf-8") as f:
        f.write(content)
    return json.dumps({"path": _rel(p), "mode": mode, "bytes_written": len(content.encode("utf-8"))})


@mcp.tool(name="make_dir", annotations={"title": "Create a directory", **WRITE})
def make_dir(path: Annotated[str, Field(description="Directory to create, relative to root")]) -> str:
    """Create a directory (including parents). Returns JSON {"path","created"}."""
    p = _resolve(path)
    existed = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    return json.dumps({"path": _rel(p), "created": not existed})


@mcp.tool(name="move", annotations={"title": "Move or rename", **DESTRUCTIVE})
def move(
    src: Annotated[str, Field(description="Source path relative to root")],
    dst: Annotated[str, Field(description="Destination path relative to root")],
) -> str:
    """Move or rename a file/folder within the sandbox. Returns JSON {"src","dst"}."""
    s, d = _resolve(src), _resolve(dst)
    if not s.exists():
        return json.dumps({"error": f"Source not found: {src}"})
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return json.dumps({"src": _rel(s), "dst": _rel(d)})


@mcp.tool(name="delete", annotations={"title": "Delete a path", **DESTRUCTIVE})
def delete(
    path: Annotated[str, Field(description="File or folder to delete, relative to root")],
    recursive: Annotated[bool, Field(description="Required true to delete a non-empty directory")] = False,
) -> str:
    """Delete a file, or a directory (recursive=true for non-empty). Irreversible.

    Returns JSON {"path","deleted"}.
    """
    p = _resolve(path)
    if not p.exists():
        return json.dumps({"error": f"Not found: {path}"})
    if p.is_dir():
        if any(p.iterdir()) and not recursive:
            return json.dumps({"error": f"Directory not empty: {path}. Pass recursive=true to delete."})
        shutil.rmtree(p) if recursive else p.rmdir()
    else:
        p.unlink()
    return json.dumps({"path": _rel(p), "deleted": True})


# ---------------------------------------------------------------------------
# Video — the slice-by-time surface
# ---------------------------------------------------------------------------
@mcp.tool(name="video_info", annotations={"title": "Probe video/audio", **RO})
def video_info(path: Annotated[str, Field(description="Media file relative to root")]) -> str:
    """Probe a media file so the model knows the time range it can sample.

    Returns JSON: {"path","duration_sec","width","height","fps","codec",
    "has_audio"}. Call this before video_frames to pick sensible timestamps.
    """
    err = _require_ffmpeg()
    if err:
        return json.dumps({"error": err})
    p = _resolve(path)
    if not p.is_file():
        return json.dumps({"error": f"Not a file: {path}"})
    try:
        info = _ffprobe(p)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    out = {"path": _rel(p), "duration_sec": round(_duration(p), 3),
           "width": None, "height": None, "fps": None, "codec": None, "has_audio": False}
    for s in info.get("streams", []):
        if s.get("codec_type") == "video" and out["codec"] is None:
            out.update(width=s.get("width"), height=s.get("height"), codec=s.get("codec_name"))
            rate = s.get("avg_frame_rate", "0/0")
            try:
                num, den = rate.split("/")
                out["fps"] = round(int(num) / int(den), 3) if int(den) else None
            except (ValueError, ZeroDivisionError):
                pass
        if s.get("codec_type") == "audio":
            out["has_audio"] = True
    return json.dumps(out, indent=2)


@mcp.tool(name="video_frame", annotations={"title": "Extract one video frame", **RO})
def video_frame(
    path: Annotated[str, Field(description="Video file relative to root")],
    timestamp: Annotated[float, Field(description="Seek position in seconds", ge=0)],
    max_dimension: Annotated[int, Field(description="Cap longest edge of the frame (px)", ge=64, le=4096)] = DEFAULT_MAX_DIM,
):
    """Return a single frame at `timestamp` as an image the model can see.

    Use for agentic seeking — probe with video_info, then narrow toward the moment
    you want (e.g. binary-search for a title card). Returns an Image, or a JSON
    error string.
    """
    err = _require_ffmpeg()
    if err:
        return json.dumps({"error": err})
    p = _resolve(path)
    if not p.is_file():
        return json.dumps({"error": f"Not a file: {path}"})
    try:
        return Image(data=_extract_frame(p, timestamp, max_dimension), format="png")
    except RuntimeError as e:
        return json.dumps({"error": f"Frame extraction failed: {e}"})


@mcp.tool(name="video_frames", annotations={"title": "Sample frames across a slice", **RO})
def video_frames(
    path: Annotated[str, Field(description="Video file relative to root")],
    start: Annotated[float, Field(description="Slice start in seconds", ge=0)] = 0.0,
    end: Annotated[Optional[float], Field(description="Slice end in seconds; omit for end of video")] = None,
    count: Annotated[int, Field(description="Number of frames evenly sampled across the slice", ge=1, le=MAX_FRAMES)] = DEFAULT_FRAMES,
    max_dimension: Annotated[int, Field(description="Cap longest edge of each frame (px)", ge=64, le=4096)] = DEFAULT_MAX_DIM,
):
    """Evenly sample `count` frames across the time slice [start, end] as images.

    This is the slice-access view of a video: one call returns an ordered set of
    frames so the model can scan a span. The first list element is a JSON summary
    naming the timestamp of each frame in order; the rest are the frame images.
    Keep count modest — every frame is encoded by the vision model in full.
    """
    err = _require_ffmpeg()
    if err:
        return [err]
    p = _resolve(path)
    if not p.is_file():
        return [json.dumps({"error": f"Not a file: {path}"})]
    try:
        dur = _duration(p)
    except RuntimeError as e:
        return [json.dumps({"error": str(e)})]
    hi = dur if end is None else min(end, dur)
    if hi <= start:
        return [json.dumps({"error": f"Empty slice: start={start}, end={hi}, duration={round(dur, 3)}"})]
    if count == 1:
        stamps = [start]
    else:
        step = (hi - start) / (count - 1)
        stamps = [round(start + i * step, 3) for i in range(count)]
    results: list = [json.dumps({"path": _rel(p), "slice": [start, round(hi, 3)],
                                 "timestamps_sec": stamps})]
    for t in stamps:
        try:
            results.append(Image(data=_extract_frame(p, t, max_dimension), format="png"))
        except RuntimeError as e:
            results.append(json.dumps({"error": f"frame at {t}s failed: {e}"}))
    return results


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="Expose one folder to a VLM over MCP.")
    ap.add_argument("--root", default=os.environ.get("MCP_ROOT", "."),
                    help="Folder to expose (all access is confined here).")
    ap.add_argument("--http", action="store_true", help="Use streamable HTTP instead of stdio.")
    ap.add_argument("--port", type=int, default=8000, help="Port for --http mode.")
    args = ap.parse_args()

    os.environ["MCP_ROOT"] = str(Path(args.root).expanduser().resolve())
    if args.http:
        mcp.run(transport="streamable_http", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
