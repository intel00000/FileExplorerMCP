"""Tool groups. Each module exposes ``register(mcp, root)``; `register_all` wires
the whole catalog onto a FastMCP instance."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..sandbox import Root
from . import explore, mutate, read, search, video


def register_all(
    mcp,
    root: Root,
    *,
    allow_write: bool = False,
    allow_delete: bool = False,
    frames_dir: Optional[Path] = None,
    max_image_dim: Optional[int] = None,
) -> None:
    """Register the tool catalog on `mcp`, all confined to `root`.

    Read tools (explore/read/search/video) are always on. The mutating tools are
    opt-in: `allow_write` enables write_file/make_dir, `allow_delete` enables
    move/delete. With both False (default) the server is strictly read-only.
    `frames_dir` is where video file-output mode materializes frames (default
    ``<root>/.filebridge_frames``). `max_image_dim`, if set, is a server-side
    ceiling clamped onto every image/frame's `max_dimension`.
    """
    explore.register(mcp, root)
    read.register(mcp, root, max_image_dim=max_image_dim)
    search.register(mcp, root)
    mutate.register(mcp, root, allow_write=allow_write, allow_delete=allow_delete)
    video.register(mcp, root, frames_dir=frames_dir, max_image_dim=max_image_dim)
