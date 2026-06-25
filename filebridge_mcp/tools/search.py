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
import time
from typing import Annotated

from pydantic import Field

from ..config import GREP_LINE_MAX, GREP_MAX_FILE, GREP_TIME_BUDGET, RO
from ..detect import detect_kind
from ..sandbox import Root


def register(mcp, root: Root) -> None:
    @mcp.tool(name="glob", annotations={"title": "Glob for paths", **RO})
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

        Guardrails keep a costly pattern from stalling the server: each line is scanned
        only up to its first ~2000 characters (so one very long line can't drive
        pathological backtracking), and the whole search is abandoned after a few
        seconds. If that budget is hit the result carries "stopped": true plus a "note"
        explaining why — anchor the pattern, avoid nested quantifiers like (a+)+, or
        narrow path_glob, then retry.
        """
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})
        matches, truncated, stopped = [], False, False
        deadline = time.monotonic() + GREP_TIME_BUDGET
        for f in root.base.glob(path_glob):
            if time.monotonic() > deadline:
                stopped = True
                break
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
                        # Scan only the head of each line: bounds worst-case regex
                        # backtracking on a pathologically long line (a ReDoS guard).
                        if rx.search(line[:GREP_LINE_MAX]):
                            matches.append({
                                "path": root.rel(f),
                                "line": n,
                                "text": line.rstrip("\n")[:400],
                            })
                            if len(matches) >= max_results:
                                truncated = True
                                break
                        # Check the wall-clock budget periodically so a slow pattern
                        # spread over many lines/files can't run unbounded.
                        if n % 512 == 0 and time.monotonic() > deadline:
                            stopped = True
                            break
            except OSError:
                continue
            if truncated or stopped:
                break
        out = {
            "pattern": pattern,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }
        if stopped:
            out["stopped"] = True
            out["note"] = (
                f"Search halted after ~{GREP_TIME_BUDGET:g}s to prevent excessive "
                f"regex backtracking (ReDoS). Results are partial - anchor the pattern, "
                f"avoid nested quantifiers like (a+)+, or narrow path_glob, then retry."
            )
        return json.dumps(out, indent=2)
