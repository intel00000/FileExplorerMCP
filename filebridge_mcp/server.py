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
from .ephemeral import ephemeral_capable
from .sandbox import Root
from .tools import register_all

INSTRUCTIONS = (
    "Sandboxed access to one folder. Orient with list_dir/stat/glob/grep "
    "before reading. read_file auto-detects type: text returns a line slice "
    "(use offset/limit to page), images and PDF-page renders return as images "
    "the model can see, video/audio return metadata only — use video_frame / "
    "video_frames to actually see footage. All paths are relative to the root. "
    "Every tool accepts an ephemeral flag. Set ephemeral=true for a call whose "
    "output you only need to read once (a directory listing, a search dump, a "
    "frame): the host keeps that result in full only for your very next reply, "
    "then replaces it with a short placeholder to save context. It is NOT sent "
    "again, so note anything you need from it in your reply before moving on, and "
    "do not call the tool again with the same arguments just to re-read it."
)


def build_server(
    root: Root,
    *,
    allow_write: bool = False,
    allow_delete: bool = False,
    frames_dir: "Path | None" = None,
    host: str = "127.0.0.1",
    port: int = 8000,
    http_path: str = "/mcp",
    auth_token: "str | None" = None,
    max_image_dim: "int | None" = None,
    image_format: "str | None" = None,
    image_quality: "int | None" = None,
    ffmpeg_timeout: "float | None" = None,
    frames_ttl: "float | None" = None,
) -> FastMCP:
    """Create a FastMCP server with the tool catalog bound to `root`.

    The server is read-only unless `allow_write` / `allow_delete` opt the
    mutating tools in (see `register_all`). `frames_dir` overrides where video
    file-output mode writes frames (default ``<root>/.filebridge_frames``).
    `host`/`port`/`http_path` configure the HTTP bind and endpoint path;
    `auth_token`, if given, requires that shared-secret bearer token on every
    HTTP request (HTTP transport only). `max_image_dim`, if set, caps every
    image/frame's longest edge server-side (overriding larger per-call requests);
    unset means no server cap — the per-call `max_dimension` alone decides, and
    omitting both returns native resolution. `image_format` / `image_quality` are
    server-side encoding defaults (png/jpeg, 1..100) that a per-call `format` /
    `quality` overrides. `ffmpeg_timeout` (seconds, None = unbounded) bounds every
    ffmpeg/ffprobe call so a pathological media file can't hang the server.
    `frames_ttl` (seconds, None/<=0 = keep) ages out old output='file' frames.
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
        streamable_http_path=http_path,
        **auth_kwargs,
    )
    # Make every tool ephemeral-capable in one place: wrap mcp.tool so each
    # @mcp.tool(...) also applies @ephemeral_capable, adding the shared `ephemeral`
    # opt-in field and its result tagging. Tools are written normally; the model is
    # told about the flag once, at the server-instruction level (INSTRUCTIONS).
    _register_tool = mcp.tool

    def _ephemeral_tool(*args, **kwargs):
        decorator = _register_tool(*args, **kwargs)
        return lambda fn: decorator(ephemeral_capable(fn))

    mcp.tool = _ephemeral_tool
    register_all(
        mcp,
        root,
        allow_write=allow_write,
        allow_delete=allow_delete,
        frames_dir=frames_dir,
        max_image_dim=max_image_dim,
        image_format=image_format,
        image_quality=image_quality,
        ffmpeg_timeout=ffmpeg_timeout,
        frames_ttl=frames_ttl,
    )
    return mcp


def _serve_http(mcp: FastMCP, host: str, port: int, cors_origins: "list[str]") -> None:
    """Serve the streamable-HTTP app, adding CORS when origins are given.

    Browser-based MCP clients are subject to the same-origin policy; without CORS
    headers a cross-origin fetch fails with a NetworkError before it ever reaches
    the server. `expose_headers=['Mcp-Session-Id']` is required so browser JS can
    read the session id that streamable HTTP returns.
    """
    import uvicorn

    app = mcp.streamable_http_app()
    if cors_origins:
        from starlette.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["Mcp-Session-Id"],
        )
    uvicorn.run(app, host=host, port=port, log_level=mcp.settings.log_level.lower())


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
        "--allow-all",
        action="store_true",
        help="Enable every mutating tool — shorthand for --allow-write --allow-delete.",
    )
    ap.add_argument(
        "--frames-dir",
        default=None,
        help="Where video output='file' mode writes frames "
        "(default <root>/.filebridge_frames). This is the only path the "
        "read-only server writes to.",
    )
    ap.add_argument(
        "--frames-ttl",
        type=float,
        default=3600.0,
        metavar="SECONDS",
        help="Age out output='file' video frames/sheets older than this many seconds "
        "(swept on the next frame write; only files this server created are removed). "
        "Set 0 to keep them indefinitely. Default: 3600.",
    )
    ap.add_argument(
        "--max-image-dimension",
        type=int,
        default=None,
        metavar="PX",
        help="Cap the longest edge of every returned image/frame (px), overriding any "
        "larger per-call request — useful for bounding vision-token / VRAM cost. "
        "Default: unset, meaning no server cap. The effective cap on each image is the "
        "smaller of this and the model's own max_dimension; if neither is set, images "
        "are returned at native resolution.",
    )
    ap.add_argument(
        "--image-format",
        choices=["png", "jpeg"],
        default=None,
        help="Default encoding for returned images/frames: png (lossless) or jpeg "
        "(smaller). The model can override per call. Default: native for read_file "
        "images, png for video frames, jpeg for contact sheets.",
    )
    ap.add_argument(
        "--image-quality",
        type=int,
        default=None,
        metavar="1-100",
        help="Default JPEG quality (1-100, higher=better) when images are encoded as "
        "jpeg. The model can override per call. Default: 85.",
    )
    ap.add_argument(
        "--ffmpeg-timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="Max seconds each ffmpeg/ffprobe call may run before it is aborted "
        "(video tools and stat on media). On timeout the tool returns an error asking "
        "the model to slow down. Set 0 to disable the limit. Default: 60.",
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
        "--http-path",
        default="/mcp",
        help="HTTP endpoint path (default /mcp). Clients connect to "
        "http://<host>:<port><path>; set / if your client posts to the root.",
    )
    ap.add_argument(
        "--cors-origin",
        action="append",
        metavar="ORIGIN",
        help="Allow browser requests from this Origin (repeatable; comma-separated "
        "OK; '*' for any). Required for browser-based MCP clients — without it a "
        "cross-origin fetch fails with NetworkError. Exposes the Mcp-Session-Id header.",
    )
    ap.add_argument(
        "--auth-token",
        default=os.environ.get("FILEBRIDGE_AUTH_TOKEN"),
        help="Require this shared-secret bearer token on HTTP requests "
        "(Authorization: Bearer <token>). Defaults to the FILEBRIDGE_AUTH_TOKEN env "
        "var (preferred — keeps the secret out of the process list). HTTP mode only.",
    )
    args = ap.parse_args()

    allow_write = args.allow_write or args.allow_all
    allow_delete = args.allow_delete or args.allow_all
    image_quality = (
        max(1, min(100, args.image_quality)) if args.image_quality is not None else None
    )
    # 0 (or negative) means "no limit / keep forever" -> None.
    ffmpeg_timeout = (
        args.ffmpeg_timeout if args.ffmpeg_timeout and args.ffmpeg_timeout > 0 else None
    )
    frames_ttl = args.frames_ttl if args.frames_ttl and args.frames_ttl > 0 else None

    cors_origins: list[str] = []
    for item in args.cors_origin or []:
        cors_origins.extend(o.strip() for o in item.split(",") if o.strip())

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
        allow_write=allow_write,
        allow_delete=allow_delete,
        frames_dir=frames_dir,
        host=args.host,
        port=args.port,
        http_path=args.http_path,
        auth_token=auth_token,
        max_image_dim=args.max_image_dimension,
        image_format=args.image_format,
        image_quality=image_quality,
        ffmpeg_timeout=ffmpeg_timeout,
        frames_ttl=frames_ttl,
    )

    enabled = ["read-only core"]
    if allow_write:
        enabled.append("write")
    if allow_delete:
        enabled.append("delete")
    if args.http:
        extras = f"auth: {'on' if auth_token else 'off'}"
        if cors_origins:
            extras += f", cors: {','.join(cors_origins)}"
        transport = f"http://{args.host}:{args.port}{args.http_path} ({extras})"
    else:
        transport = "stdio"
    print(
        f"filebridge_mcp: root={root.base} | enabled: {', '.join(enabled)} | {transport}",
        file=sys.stderr,
    )

    if args.http:
        _serve_http(mcp, args.host, args.port, cors_origins)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
