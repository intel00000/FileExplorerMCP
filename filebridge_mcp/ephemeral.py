"""Ephemeral (one-time-in-context) tool results.

One reusable opt-in field, applied to bloated tools via the ``@ephemeral_capable``
decorator so no tool body changes. When the model sets ``ephemeral=true`` on a
call, the tool's return value is wrapped in a ``CallToolResult`` stamped with
result-level ``_meta={"ephemeral": True}``; the host (WebUI) then keeps it only
until the model's next message and afterwards collapses it to a short placeholder
to reclaim context.

Result-level ``_meta`` rides through FastMCP untouched: returning a
``CallToolResult`` short-circuits the normal content/structured-output
conversion, so the dict is serialized verbatim under the wire key ``_meta``.
"""

from __future__ import annotations

import functools
import inspect
from typing import Annotated, Any, Callable, get_type_hints

from mcp.server.fastmcp import Image
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

# The single field used to mark a result ephemeral, shared by every bloated tool.
EPHEMERAL = Field(
    description=(
        "If true, mark this result one-time: the host keeps it in full only until "
        "your next message, then collapses it to a short placeholder to reclaim "
        "context. Set this when you only need to read the output once (a directory "
        "listing, a search dump, a video frame); re-call the tool to view it again."
    )
)
Ephemeral = Annotated[bool, EPHEMERAL]


def _to_blocks(value: Any) -> list:
    """Normalize a tool return value into a list of MCP content blocks.

    Mirrors FastMCP's own coercion for the shapes these tools return: ``str`` ->
    TextContent, ImageContent passthrough, FastMCP ``Image`` -> ImageContent, and
    a (possibly mixed) list -> flattened. Anything else is stringified.
    """
    if isinstance(value, list):
        blocks: list = []
        for item in value:
            blocks.extend(_to_blocks(item))
        return blocks
    if isinstance(value, ImageContent):
        return [value]
    if isinstance(value, Image):
        return [value.to_image_content()]
    if isinstance(value, TextContent):
        return [value]
    if isinstance(value, str):
        return [TextContent(type="text", text=value)]
    return [TextContent(type="text", text=str(value))]


def finalize(result: Any, ephemeral: bool) -> Any:
    """Return ``result`` unchanged, or wrapped in a CallToolResult carrying
    result-level ``_meta={"ephemeral": True}`` when the model opted in."""
    if not ephemeral:
        return result
    return CallToolResult(content=_to_blocks(result), _meta={"ephemeral": True})


def ephemeral_capable(fn: Callable) -> Callable:
    """Add the shared model-facing ``ephemeral`` flag to a FastMCP tool, in one
    place, without editing its body.

    The flag is appended to the tool's input schema (so the model can set it), and
    when it is set the tool's return value is routed through ``finalize``. The
    original return annotation is dropped so a ``CallToolResult`` return does not
    clash with a structured-output schema inferred from that annotation. Apply it
    directly under ``@mcp.tool(...)`` so FastMCP introspects the augmented
    signature::

        @mcp.tool(name="list_dir", ...)
        @ephemeral_capable
        def list_dir(...): ...
    """
    # Resolve annotations now via the tool's OWN module globals. Tool modules use
    # `from __future__ import annotations`, so their param annotations are lazy
    # strings (e.g. "Annotated[int, Field(le=MAX_LINES)]"); if left as strings,
    # FastMCP would resolve them against this module's globals and miss the tool
    # module's names. Resolving here yields real types on the augmented signature.
    try:
        hints = get_type_hints(fn, include_extras=True)
    except Exception:
        hints = {}

    sig = inspect.signature(fn)
    params = [
        p.replace(annotation=hints.get(p.name, p.annotation))
        for p in sig.parameters.values()
    ]
    params.append(
        inspect.Parameter(
            "ephemeral",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Ephemeral,
        )
    )
    # No return annotation -> FastMCP builds no structured-output schema, so a
    # CallToolResult return (the ephemeral case) does not clash with one.
    new_sig = sig.replace(parameters=params, return_annotation=inspect.Signature.empty)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        ephemeral = kwargs.pop("ephemeral", False)
        return finalize(fn(*args, **kwargs), ephemeral)

    # inspect.signature() honors __signature__ over __wrapped__, so FastMCP builds
    # the input schema from this augmented, fully-resolved signature.
    wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
    wrapper.__annotations__ = {p.name: p.annotation for p in params}
    return wrapper
