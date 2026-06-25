"""Tool groups. Each module exposes ``register(mcp, root)``; `register_all` wires
the whole catalog onto a FastMCP instance."""

from __future__ import annotations

from ..sandbox import Root
from . import explore, mutate, read, search, video


def register_all(mcp, root: Root) -> None:
    """Register every tool group on `mcp`, all confined to `root`."""
    explore.register(mcp, root)
    read.register(mcp, root)
    search.register(mcp, root)
    mutate.register(mcp, root)
    video.register(mcp, root)
