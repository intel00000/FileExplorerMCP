"""filebridge_mcp — expose one folder to a vision-language model over MCP.

`read_file` is a *renderer*, not a byte pipe: it detects each file's type and
projects it into the only two channels a VLM can consume — text or images — plus
timestamp-addressed video frame extraction. See ``filebridge_mcp_design.md``.

The package is layered so the security core (`sandbox`, `detect`, `config`) imports
without the MCP SDK and can be unit-tested in a minimal environment; only `server`
and the `tools`/`media.images` modules pull in `mcp`.
"""

from __future__ import annotations

from .sandbox import Root

__version__ = "0.1.0"
__all__ = ["Root", "main", "__version__"]


def main() -> None:
    """Lazy entrypoint — defers the MCP SDK import to call time."""
    from .server import main as _main

    _main()
