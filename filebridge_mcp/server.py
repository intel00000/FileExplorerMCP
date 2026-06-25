"""Server assembly and CLI entrypoint.

`build_server(root)` constructs a FastMCP instance and registers the full tool
catalog against an injected `Root`. `main()` parses CLI args, builds the root, and
runs the chosen transport (stdio by default, streamable HTTP with --http).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .sandbox import Root
from .tools import register_all

INSTRUCTIONS = (
    "Sandboxed access to one folder. Orient with list_dir/stat/glob/grep "
    "before reading. read_file auto-detects type: text returns a line slice "
    "(use offset/limit to page), images and PDF-page renders return as images "
    "the model can see, video/audio return metadata only — use video_frame / "
    "video_frames to actually see footage. All paths are relative to the root."
)


def build_server(root: Root) -> FastMCP:
    """Create a FastMCP server with every tool bound to `root`."""
    mcp = FastMCP("filebridge_mcp", instructions=INSTRUCTIONS)
    register_all(mcp, root)
    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(description="Expose one folder to a VLM over MCP.")
    ap.add_argument("--root", default=os.environ.get("MCP_ROOT", "."),
                    help="Folder to expose (all access is confined here). "
                         "Falls back to the MCP_ROOT env var, then the cwd.")
    ap.add_argument("--http", action="store_true", help="Use streamable HTTP instead of stdio.")
    ap.add_argument("--port", type=int, default=8000, help="Port for --http mode.")
    args = ap.parse_args()

    root = Root(Path(args.root))
    mcp = build_server(root)
    if args.http:
        mcp.run(transport="streamable_http", port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
