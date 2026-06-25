"""The sandbox security core (design decision D5: resolve-then-contain).

`Root` is the single object that confines every path operation to one folder.
It is injected into the tool modules rather than read from a process-global, so
the server can be unit-tested with throwaway roots and, in principle, run more
than one root in a single process.

Containment rule: a caller-supplied path is treated as *relative*, fully resolved
(following symlinks), then re-checked against the root. Resolving before checking
is what closes the symlink-escape hole — a link pointing outside the root resolves
outside and fails containment.

TOCTOU note: every tool calls ``resolve()`` immediately before it touches the
path, and the model has no primitive that creates symlinks, so the window between
the containment check and the I/O is minimal in this threat model. Fully closing
it would require fd-based, no-follow opens (``O_NOFOLLOW`` / ``openat``), which are
not portable to Windows; that stricter posture is left as future work.
"""

from __future__ import annotations

import os
from pathlib import Path

# Windows reserved device names. On Windows these resolve to DEVICES regardless of
# the directory they appear in (e.g. ``read_file("CON")`` would open the console and
# can hang; ``COM1``/``LPT1`` touch hardware), and they pass a purely lexical
# containment check because the name sits under the root. They are rejected on
# Windows only — on POSIX these are perfectly legal filenames and stay accessible.
_WIN_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _is_windows() -> bool:
    """Whether the server is running on Windows (indirection so tests can patch it)."""
    return os.name == "nt"


def _has_reserved_component(cleaned: str) -> bool:
    """True if any path component is a Windows reserved device name.

    Windows ignores a trailing extension, and trailing dots/spaces, when matching
    device names — ``CON``, ``CON.txt`` and ``CON `` all denote the console — so we
    compare the component's stem (text before the first dot), stripped and uppercased.
    """
    for part in cleaned.split("/"):
        if part and part.split(".", 1)[0].strip().upper() in _WIN_RESERVED:
            return True
    return False


class Root:
    """A sandbox root. All public methods keep access inside `self.base`."""

    def __init__(self, base: str | Path) -> None:
        self.base = Path(base).expanduser().resolve()

    def is_within(self, p: str | Path) -> bool:
        """True if `p` (after symlink resolution) is the root or a descendant.

        Used both by `resolve()` and to post-filter glob/grep results — which
        enumerate via ``base.glob()`` and could otherwise surface paths reached
        through a symlink that escapes the root.
        """
        rp = Path(p).resolve()
        return rp == self.base or self.base in rp.parents

    def resolve(self, rel: str | Path) -> Path:
        """Resolve a caller-supplied relative path, confined under the root.

        Leading separators are stripped (input is always treated as relative),
        the path is fully resolved (following symlinks), then containment is
        re-checked. Raises ``ValueError`` on escape.
        """
        cleaned = str(rel).replace("\\", "/").lstrip("/")
        if _is_windows() and _has_reserved_component(cleaned):
            raise ValueError(
                f"Path '{rel}' refers to a reserved Windows device name "
                f"(CON, PRN, AUX, NUL, COM1-9, LPT1-9). These are devices, not "
                f"files in the root, and are not accessible."
            )
        target = (self.base / cleaned).resolve()
        if not self.is_within(target):
            raise ValueError(
                f"Path '{rel}' escapes the sandbox root. Use a path inside the "
                f"root; '..' and absolute paths outside the root are not allowed."
            )
        return target

    def rel(self, p: Path) -> str:
        """Present a POSIX-style path relative to the root (OS-independent)."""
        try:
            return p.relative_to(self.base).as_posix() or "."
        except ValueError:
            return p.as_posix()

    def scrub(self, text: str) -> str:
        """Redact the absolute root prefix from a message.

        Exceptions from Pillow/PyMuPDF/ffmpeg often embed the absolute path we
        passed them, which would disclose the server's filesystem layout. Replacing
        the base prefix leaves only the root-relative remainder in error strings.
        """
        base = str(self.base)
        return text.replace(base + os.sep, "").replace(base, ".")
