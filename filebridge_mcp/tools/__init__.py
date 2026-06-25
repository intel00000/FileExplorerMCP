"""Tool groups. Each module exposes ``register(mcp, root)``; `register_all` wires
the whole catalog onto a FastMCP instance."""

from __future__ import annotations

from ..sandbox import Root
from . import explore, mutate, read, search, video


def register_all(mcp, root: Root, *, allow_write: bool = False, allow_delete: bool = False) -> None:
    """Register the tool catalog on `mcp`, all confined to `root`.

    Read tools (explore/read/search/video) are always on. The mutating tools are
    opt-in: `allow_write` enables write_file/make_dir, `allow_delete` enables
    move/delete. With both False (default) the server is strictly read-only.
    """
    explore.register(mcp, root)
    read.register(mcp, root)
    search.register(mcp, root)
    mutate.register(mcp, root, allow_write=allow_write, allow_delete=allow_delete)
    video.register(mcp, root)
