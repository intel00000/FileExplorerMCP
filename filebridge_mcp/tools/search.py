"""Search tools: glob, grep.

Both enumerate via ``root.base.glob(pattern)``, which — unlike the per-call
``root.resolve()`` used elsewhere — can follow a symlinked directory that points
outside the sandbox. So every yielded path is re-checked with ``root.is_within``
before being returned, preserving the D5 containment guarantee on reads too.

`grep` searches *text* files only. The original draft also accepted "pdf" and
scanned it as UTF-8, which matches against compressed PDF bytes rather than page
text — meaningless results with misleading line numbers. PDFs are skipped here;
use `read_file` (PyMuPDF) to read their text.
"""

from __future__ import annotations

import json
import re
from typing import Annotated

from pydantic import Field

from ..config import GREP_MAX_FILE, RO
from ..detect import detect_kind
from ..ephemeral import ephemeral_capable
from ..sandbox import Root


def register(mcp, root: Root) -> None:
    @mcp.tool(name="glob", annotations={"title": "Glob for paths", **RO})
    @ephemeral_capable
    def glob(
        pattern: Annotated[
            str,
            Field(
                description="Glob relative to root, e.g. '**/*.srt' or 'movies/*.mp4'"
            ),
        ],
        max_results: Annotated[
            int, Field(description="Cap on paths returned", ge=1, le=2000)
        ] = 500,
    ) -> str:
        """Find paths by glob pattern (supports ** for recursion).

        Returns JSON: {"pattern","count","truncated","paths":[...]}.
        Paths reached via a symlink that escapes the root are skipped.

        A wide glob can return many paths; pass ephemeral=true to keep the result
        in context only until your next reply, then let the host drop it.
        """
        paths, truncated = [], False
        for m in root.base.glob(pattern):
            if not root.is_within(m):
                continue
            if len(paths) >= max_results:
                truncated = True
                break
            paths.append(root.rel(m))
        return json.dumps(
            {
                "pattern": pattern,
                "count": len(paths),
                "truncated": truncated,
                "paths": paths,
            },
            indent=2,
        )

    @mcp.tool(name="grep", annotations={"title": "Grep file contents", **RO})
    @ephemeral_capable
    def grep(
        pattern: Annotated[str, Field(description="Python regex to search for")],
        path_glob: Annotated[
            str, Field(description="Restrict search to files matching this glob")
        ] = "**/*",
        max_results: Annotated[
            int, Field(description="Cap on matching lines returned", ge=1, le=1000)
        ] = 200,
        ignore_case: Annotated[
            bool, Field(description="Case-insensitive match")
        ] = False,
    ) -> str:
        """Search text-file contents for a regex; pairs with read_file's line offsets.

        Returns JSON: {"pattern","count","truncated","matches":[{"path","line","text"}]}.
        Non-text files (including PDFs) and files over ~5 MB are skipped.

        A broad search can dump many matching lines; pass ephemeral=true to keep
        the result in context only until your next reply, then let the host drop it.
        """
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})
        matches, truncated = [], False
        for f in root.base.glob(path_glob):
            if not root.is_within(f) or not f.is_file():
                continue
            try:
                if f.stat().st_size > GREP_MAX_FILE:
                    continue
            except OSError:
                continue
            if detect_kind(f)[0] != "text":
                continue
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for n, line in enumerate(fh, 1):
                        if rx.search(line):
                            matches.append(
                                {
                                    "path": root.rel(f),
                                    "line": n,
                                    "text": line.rstrip("\n")[:400],
                                }
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        return json.dumps(
            {
                "pattern": pattern,
                "count": len(matches),
                "truncated": truncated,
                "matches": matches,
            },
            indent=2,
        )
