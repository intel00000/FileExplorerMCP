"""Mutating tools: write_file, make_dir (WRITE) and move, delete (DESTRUCTIVE).

These are **off by default**: the server starts read-only and registers nothing
here unless the operator opts in at launch (``--allow-write`` for write_file /
make_dir, ``--allow-delete`` for move / delete). A tool that is never registered
is invisible to the model — a stronger guarantee than relying on the host to honor
the ``destructiveHint`` annotations. `delete` additionally requires an explicit
``recursive`` flag for a non-empty directory (design §7).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ..config import DESTRUCTIVE, WRITE
from ..sandbox import Root


def register(
    mcp, root: Root, *, allow_write: bool = False, allow_delete: bool = False
) -> None:
    """Register mutating tools, gated by the launch flags.

    allow_write  -> write_file, make_dir
    allow_delete -> move, delete   (the destructive verbs)
    With both False (the default) this registers nothing and the server is
    strictly read-only.
    """
    if allow_write:
        _register_write(mcp, root)
    if allow_delete:
        _register_delete(mcp, root)


def _register_write(mcp, root: Root) -> None:
    @mcp.tool(name="write_file", annotations={"title": "Write a text file", **WRITE})
    def write_file(
        path: Annotated[str, Field(description="Destination file relative to root")],
        content: Annotated[str, Field(description="Text content to write")],
        mode: Annotated[
            str,
            Field(
                description="'overwrite' (default) or 'append'",
                pattern="^(overwrite|append)$",
            ),
        ] = "overwrite",
    ) -> str:
        """Write or append UTF-8 text to a file (parent dirs are created as needed).

        Binary writes are an extension point (accept base64 + decode). Returns JSON
        {"path","mode","bytes_written"}.
        """
        p = root.resolve(path)
        if p.is_dir():
            return json.dumps({"error": f"Is a directory, not a file: {path}"})
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a" if mode == "append" else "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            return json.dumps({
                "error": root.scrub(f"Could not write '{root.rel(p)}': {e}")
            })
        return json.dumps({
            "path": root.rel(p),
            "mode": mode,
            "bytes_written": len(content.encode("utf-8")),
        })

    @mcp.tool(name="make_dir", annotations={"title": "Create a directory", **WRITE})
    def make_dir(
        path: Annotated[
            str, Field(description="Directory to create, relative to root")
        ],
    ) -> str:
        """Create a directory (including parents). Returns JSON {"path","created"}."""
        p = root.resolve(path)
        if p.exists() and not p.is_dir():
            return json.dumps({"error": f"Path exists and is not a directory: {path}"})
        existed = p.is_dir()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return json.dumps({
                "error": root.scrub(f"Could not create '{root.rel(p)}': {e}")
            })
        return json.dumps({"path": root.rel(p), "created": not existed})


def _register_delete(mcp, root: Root) -> None:
    @mcp.tool(name="move", annotations={"title": "Move or rename", **DESTRUCTIVE})
    def move(
        src: Annotated[str, Field(description="Source path relative to root")],
        dst: Annotated[str, Field(description="Destination path relative to root")],
    ) -> str:
        """Move or rename a file/folder within the sandbox. Returns JSON {"src","dst"}."""
        s, d = root.resolve(src), root.resolve(dst)
        if not s.exists():
            return json.dumps({"error": f"Source not found: {src}"})
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            # shutil.move returns the real final path (handles "move into existing dir",
            # where the file lands at d/<name> rather than at d).
            final = shutil.move(str(s), str(d))
        except OSError as e:
            return json.dumps({
                "error": root.scrub(f"Could not move '{root.rel(s)}': {e}")
            })
        return json.dumps({"src": root.rel(s), "dst": root.rel(Path(final))})

    @mcp.tool(name="delete", annotations={"title": "Delete a path", **DESTRUCTIVE})
    def delete(
        path: Annotated[
            str, Field(description="File or folder to delete, relative to root")
        ],
        recursive: Annotated[
            bool, Field(description="Required true to delete a non-empty directory")
        ] = False,
    ) -> str:
        """Delete a file, or a directory (recursive=true for non-empty). Irreversible.

        Returns JSON {"path","deleted"}.
        """
        p = root.resolve(path)
        if p == root.base:
            return json.dumps({"error": "Refusing to delete the sandbox root itself."})
        if not p.exists():
            return json.dumps({"error": f"Not found: {path}"})
        if p.is_dir():
            if any(p.iterdir()) and not recursive:
                return json.dumps({
                    "error": f"Directory not empty: {path}. Pass recursive=true to delete."
                })
            shutil.rmtree(p) if recursive else p.rmdir()
        else:
            p.unlink()
        return json.dumps({"path": root.rel(p), "deleted": True})
