# filebridge_mcp

A standalone [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes **one host folder** (Windows or Linux) to a vision-language model as an
FTP-like surface: explore, read, write, search, plus timestamp-addressed video
frame extraction.

The defining idea (design decision **D1**): **a VLM can only consume text and
images.** So `read_file` is not a byte pipe — it is a *renderer* that detects each
file's type and projects it into one of the two channels the model can use. Text
files return a line slice; images and rendered PDF pages return as images the
vision encoder sees; video returns metadata plus on-demand extracted frames.

See [filebridge_mcp_design.md](filebridge_mcp_design.md) for the full design,
security model, and open questions.

## Quickstart (uv)

```bash
uv sync                 # core + dev (pytest)
uv sync --extra all     # also Pillow (image downscale) + PyMuPDF (PDF)
uv run filebridge-mcp --root /path/to/folder
```

Or with plain pip:

```bash
pip install "mcp[cli]"          # required
pip install pillow pymupdf      # optional: image downscale + PDF text/render
# ffmpeg + ffprobe on PATH      # optional: video tools
python -m filebridge_mcp --root /path/to/folder
```

Remote / multi-client (no auth — see Security):

```bash
uv run filebridge-mcp --root /path/to/folder --http --port 8000
```

The root may also be supplied via the `MCP_ROOT` environment variable; `--root`
takes precedence.

## Host configuration (stdio)

```json
{
  "filebridge": {
    "command": "uv",
    "args": ["run", "filebridge-mcp", "--root", "/path/to/folder"]
  }
}
```

(Or `"command": "python", "args": ["-m", "filebridge_mcp", "--root", "..."]`.)

## Tool catalog

| Group   | Tools | Notes |
|---------|-------|-------|
| Explore | `list_dir`, `stat` | tree listing with detected kinds; cheap metadata (probes media when ffmpeg present) |
| Read    | `read_file`, `read_bytes` | type-dispatched projection; arbitrary byte hexdump |
| Search  | `glob`, `grep` | both re-checked for sandbox containment; grep is text-only |
| Mutate  | `write_file`, `make_dir`, `move`, `delete` | `move`/`delete` carry `destructiveHint` |
| Video   | `video_info`, `video_frame`, `video_frames` | probe, agentic single-frame seek, time-slice frame set |

## Module layout

```
filebridge_mcp/
├── config.py        constants, extension sets, magic table, annotation presets
├── sandbox.py       Root — the resolve-then-contain security core (D5)
├── detect.py        type detection + hexdump (SDK-free, testable)
├── deps.py          optional-dependency flags + actionable messages (D6)
├── media/
│   ├── images.py    downscale → MCP Image
│   └── video.py     ffprobe / duration / frame extraction (SDK-free)
├── tools/           one module per group, each exposes register(mcp, root)
│   ├── explore.py  read.py  search.py  mutate.py  video.py
│   └── __init__.py  register_all(mcp, root)
└── server.py        build_server(root) + CLI main()
```

`config`, `sandbox`, `detect`, `deps`, and `media.video` import **without** the MCP
SDK, so the security core can be tested in a minimal environment (see `tests/`).

## Security

All access is confined to `--root` via *resolve-then-contain* (D5): every path —
including `glob`/`grep` results reached through symlinks — is fully resolved and
re-checked against the root before use. Still, the server grants full read/write
**within** the root: point it only at a folder you are willing to expose. HTTP mode
has **no authentication** in this version — do not expose it on an untrusted
network without putting auth in front of it. Treat file *contents* as untrusted
input to the model (prompt-injection risk).

## Tests

```bash
uv run pytest
```

The included tests cover the sandbox containment rules (including symlink escape)
and type detection, and run without the MCP SDK installed.

## Dependency matrix

| Capability | Requirement | If missing |
|---|---|---|
| Core (explore, text, binary, write, search) | `mcp` | — (required) |
| Image downscaling | Pillow | image returned at native resolution |
| PDF text & page rendering | PyMuPDF (`fitz`) | PDF tools return an actionable error |
| Video info & frame extraction | `ffmpeg` + `ffprobe` on PATH | video tools return an actionable error |
