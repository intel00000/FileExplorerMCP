"""Explore tools: list_dir, stat."""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from .. import deps
from ..config import RO
from ..detect import detect_kind
from ..media.video import duration, ffprobe
from ..sandbox import Root


def register(mcp, root: Root) -> None:
    @mcp.tool(name="list_dir", annotations={"title": "List directory", **RO})
    def list_dir(
        path: Annotated[
            str, Field(description="Folder relative to root, e.g. '.' or 'movies/2024'")
        ] = ".",
        depth: Annotated[
            int,
            Field(description="Recursion depth (1 = immediate children)", ge=1, le=5),
        ] = 1,
        max_entries: Annotated[
            int, Field(description="Cap on entries returned", ge=1, le=2000)
        ] = 500,
    ) -> str:
        """List a directory as a tree, for orientation before reading files.

        Returns JSON: {"path", "entries":[{"path","type","size","mtime"}], "truncated"}.
        type is "dir" or a detected file kind (text/image/pdf/video/audio/...).
        """
        base = root.resolve(path)
        if not base.is_dir():
            return json.dumps({"error": f"Not a directory: {path}"})
        entries, truncated = [], False
        stack = [(base, 0)]
        while stack:
            cur, d = stack.pop(0)
            try:
                children = sorted(
                    cur.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())
                )
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
                entries.append(
                    {
                        "path": root.rel(child),
                        "type": "dir" if is_dir else detect_kind(child)[0],
                        "size": None if is_dir else size,
                        "mtime": mtime,
                    }
                )
                if is_dir and d + 1 < depth:
                    stack.append((child, d + 1))
            if truncated:
                break
        return json.dumps(
            {"path": root.rel(base), "entries": entries, "truncated": truncated},
            indent=2,
        )

    @mcp.tool(name="stat", annotations={"title": "Stat a path", **RO})
    def stat(
        path: Annotated[str, Field(description="File or folder relative to root")],
    ) -> str:
        """Cheap metadata for one path — call this before read_file to size up a file.

        Returns JSON: {"path","exists","is_dir","kind","mime","size","mtime"}.
        For video/audio, also includes duration_sec / width / height when probable.
        """
        p = root.resolve(path)
        if not p.exists():
            return json.dumps({"path": path, "exists": False})
        st = p.stat()
        out = {
            "path": root.rel(p),
            "exists": True,
            "is_dir": p.is_dir(),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        }
        if p.is_dir():
            out["kind"] = "dir"
            return json.dumps(out, indent=2)
        kind, mime = detect_kind(p)
        out["kind"], out["mime"] = kind, mime
        if kind in ("video", "audio") and not deps.require_ffmpeg():
            try:
                info = ffprobe(p)
                out["duration_sec"] = round(duration(p), 3)
                for s in info.get("streams", []):
                    if s.get("codec_type") == "video":
                        out["width"], out["height"] = s.get("width"), s.get("height")
                        out["codec"] = s.get("codec_name")
                        break
            except RuntimeError:
                pass
        return json.dumps(out, indent=2)
