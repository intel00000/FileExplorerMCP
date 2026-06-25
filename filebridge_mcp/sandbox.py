"""The sandbox security core (design decision D5: resolve-then-contain).

`Root` is the single object that confines every path operation to one folder.
It is injected into the tool modules rather than read from a process-global, so
the server can be unit-tested with throwaway roots and, in principle, run more
than one root in a single process.

Containment rule: a caller-supplied path is treated as *relative*, fully resolved
(following symlinks), then re-checked against the root. Resolving before checking
is what closes the symlink-escape hole — a link pointing outside the root resolves
outside and fails containment.
"""

from __future__ import annotations

from pathlib import Path


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
