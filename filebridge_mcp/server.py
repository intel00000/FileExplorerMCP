"""Server assembly and CLI entrypoint.

`build_server(root)` constructs a FastMCP instance and registers the full tool
catalog against an injected `Root`. `main()` parses CLI args, builds the root, and
runs the chosen transport (stdio by default, streamable HTTP with --http).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .auth import StaticTokenVerifier, auth_settings
from .sandbox import Root
from .tools import register_all

INSTRUCTIONS = (
    "Sandboxed access to one folder. Orient with list_dir/stat/glob/grep "
    "before reading. read_file auto-detects type: text returns a line slice "
    "(use offset/limit to page), images and PDF-page renders return as images "
    "the model can see, video/audio return metadata only — use video_frame / "
    "video_frames to actually see footage. All paths are relative to the root."
)


def build_server(
    root: Root,
    *,
    allow_write: bool = False,
    allow_delete: bool = False,
    frames_dir: "Path | None" = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_token: "str | None" = None,
) -> FastMCP:
    """Create a FastMCP server with the tool catalog bound to `root`.

    The server is read-only unless `allow_write` / `allow_delete` opt the
    mutating tools in (see `register_all`). `frames_dir` overrides where video
    file-output mode writes frames (default ``<root>/.filebridge_frames``).
    `host`/`port` configure the HTTP bind; `auth_token`, if given, requires that
    shared-secret bearer token on every HTTP request (HTTP transport only).
    """
    auth_kwargs: dict = {}
    if auth_token:
        auth_kwargs["token_verifier"] = StaticTokenVerifier(auth_token)
        auth_kwargs["auth"] = auth_settings(host, port)
    mcp = FastMCP(
        "filebridge_mcp",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        **auth_kwargs,
    )
    register_all(
        mcp,
        root,
        allow_write=allow_write,
        allow_delete=allow_delete,
        frames_dir=frames_dir,
    )
    return mcp


def main() -> None:
    ap = argparse.ArgumentParser(description="Expose one folder to a VLM over MCP.")
    ap.add_argument(
        "--root",
        default=os.environ.get("MCP_ROOT", "."),
        help="Folder to expose (all access is confined here). "
        "Falls back to the MCP_ROOT env var, then the cwd.",
    )
    ap.add_argument(
        "--allow-write",
        action="store_true",
        help="Enable write_file / make_dir (off by default — server is read-only).",
    )
    ap.add_argument(
        "--allow-delete",
        action="store_true",
        help="Enable move / delete, the destructive verbs (off by default).",
    )
    ap.add_argument(
        "--frames-dir",
        default=None,
        help="Where video output='file' mode writes frames "
        "(default <root>/.filebridge_frames). This is the only path the "
        "read-only server writes to.",
    )
    ap.add_argument(
        "--http", action="store_true", help="Use streamable HTTP instead of stdio."
    )
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for --http mode (default 127.0.0.1 = local only; "
        "use 0.0.0.0 to accept remote connections — no auth, see README/Security).",
    )
    ap.add_argument("--port", type=int, default=8000, help="Port for --http mode.")
    ap.add_argument(
        "--auth-token",
        default=os.environ.get("FILEBRIDGE_AUTH_TOKEN"),
        help="Require this shared-secret bearer token on HTTP requests "
        "(Authorization: Bearer <token>). Defaults to the FILEBRIDGE_AUTH_TOKEN env "
        "var (preferred — keeps the secret out of the process list). HTTP mode only.",
    )
    args = ap.parse_args()

    if args.auth_token and not args.http:
        print(
            "filebridge_mcp: warning: --auth-token is ignored without --http "
            "(stdio has no request headers).",
            file=sys.stderr,
        )

    root = Root(Path(args.root))
    frames_dir = (
        Path(args.frames_dir).expanduser().resolve() if args.frames_dir else None
    )
    # Auth only applies to HTTP; don't attach the OAuth machinery for stdio.
    auth_token = args.auth_token if args.http else None
    mcp = build_server(
        root,
        allow_write=args.allow_write,
        allow_delete=args.allow_delete,
        frames_dir=frames_dir,
        host=args.host,
        port=args.port,
        auth_token=auth_token,
    )

    enabled = ["read-only core"]
    if args.allow_write:
        enabled.append("write")
    if args.allow_delete:
        enabled.append("delete")
    if args.http:
        transport = (
            f"http://{args.host}:{args.port} (auth: {'on' if auth_token else 'off'})"
        )
    else:
        transport = "stdio"
    print(
        f"filebridge_mcp: root={root.base} | enabled: {', '.join(enabled)} | {transport}",
        file=sys.stderr,
    )

    if args.http:
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
