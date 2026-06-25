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
uv run filebridge-mcp --root /path/to/folder                      # read-only
uv run filebridge-mcp --root /path/to/folder --allow-write        # + write_file/make_dir
uv run filebridge-mcp --root /path/to/folder --allow-write --allow-delete   # + move/delete
uv run filebridge-mcp --root /path/to/folder --allow-all          # + every mutating tool
```

**The server is read-only by default.** The mutating tools are not even registered
unless you opt in at launch: `--allow-write` enables `write_file`/`make_dir`,
`--allow-delete` enables `move`/`delete`, and `--allow-all` enables both. An
unregistered tool is invisible to the model — a stronger guarantee than trusting
the host to honor `destructiveHint`.

Or with plain pip:

```bash
pip install "mcp[cli]"          # required
pip install pillow pymupdf      # optional: image downscale + PDF text/render
# ffmpeg + ffprobe on PATH      # optional: video tools
python -m filebridge_mcp --root /path/to/folder
```

Remote / multi-client (no auth — see Security):

```bash
uv run filebridge-mcp --root /path/to/folder --http --port 8000              # binds 127.0.0.1 (local only)
uv run filebridge-mcp --root /path/to/folder --http --host 0.0.0.0 --port 8000  # accept remote connections
FILEBRIDGE_AUTH_TOKEN=$(openssl rand -hex 16) \
  uv run filebridge-mcp --root /path/to/folder --http --host 0.0.0.0 --port 8000  # + require a bearer token
```

`--http` binds `127.0.0.1` by default (reachable only from the same machine);
pass `--host 0.0.0.0` (or a specific interface IP) to accept remote connections.
Optionally require a shared-secret bearer token with `--auth-token <token>` (or the
`FILEBRIDGE_AUTH_TOKEN` env var — preferred, since CLI args are visible in the
process list); clients then send `Authorization: Bearer <token>` and anything else
gets `401`. Auth applies to HTTP only.

**Endpoint path:** the MCP endpoint is at **`/mcp`**, so point your client at
`http://<host>:<port>/mcp` (not the bare host). Use `--http-path /` if your client
insists on posting to the root. The startup banner prints the exact URL.

**Browser-based clients (CORS):** a web MCP client running on a different origin
(e.g. `http://127.0.0.1:8080`) is blocked by the same-origin policy — the fetch
fails with `NetworkError` and no request reaches the server. Allow it with
`--cors-origin`:

```bash
uv run filebridge-mcp --root /path --http --cors-origin http://127.0.0.1:8080  # one origin
uv run filebridge-mcp --root /path --http --cors-origin '*'                    # any origin (dev only)
```

`--cors-origin` is repeatable (or comma-separated) and exposes the `Mcp-Session-Id`
header browsers need. Non-browser clients (Claude Desktop, llama-server, curl) don't
need it.

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
| Mutate  | `write_file`, `make_dir`, `move`, `delete` | **opt-in only** — `--allow-write` / `--allow-delete`; `move`/`delete` carry `destructiveHint` |
| Video   | `video_info`, `video_frame`, `video_frames`, `video_contact_sheet` | probe; single-frame seek (by seconds or `percent`); frame set (even slice or explicit `timestamps`); N frames tiled into one labeled image |

### File types & return formats

`read_file` is the type-dispatched reader: it detects each file's **kind** (magic
bytes → extension → UTF-8 sniff) and projects it into one of two channels — a
**JSON string** (text channel) or an **`ImageContent`** block (vision channel).
Errors are always returned as JSON `{"error": "…"}`, never raised, so the model
can read and react.

| kind | detected by | `read_file` returns |
|------|-------------|---------------------|
| **text** | UTF-8 sniff of the first 4 KB (a NUL byte ⇒ not text) | **JSON** `{path, kind:"text", total_lines, offset, returned_lines, next_offset, content}` — a streamed line window `[offset, offset+limit)`; page forward with `next_offset` (null when exhausted) |
| **image** | PNG/JPEG/GIF/BMP magic, or image ext (`.webp/.tiff/…`) | **Image**, downscaled to `max_dimension`, `_meta={path, kind:"image"}` (mime preserved: png/jpeg/gif/webp/bmp/tiff). Corrupt/undecodable ⇒ JSON `{error}` |
| **pdf** | `%PDF` magic | default → **JSON** `{path, kind:"pdf", total_pages, offset, next_offset, content}` (page-range *text*). With `render_page=true` → **Image** of the page at `offset`, `_meta={path, kind:"pdf_page", page}`. No PyMuPDF ⇒ JSON `{error}` |
| **office** | `.docx/.pptx/.xlsx` + `PK\x03\x04` zip magic | **JSON** note `{path, kind:"office", mime, note}` — not parsed (extension point) |
| **archive** | `.zip/.tar/…` or `PK\x03\x04` zip magic | **JSON** note `{path, kind:"archive", note}` — entries not listed (extension point) |
| **video** | video extension (`.mp4/.mkv/.mov/…`) | **JSON** note `{path, kind:"video", mime, note}` → use `video_frame` / `video_frames` / `video_contact_sheet` to see footage |
| **audio** | audio extension (`.mp3/.wav/.flac/…`) | **JSON** note `{path, kind:"audio", mime, note}` — transcription is an extension point |
| **binary** | fallthrough | **JSON** `{path, kind:"binary", mime, size, shown_bytes, hexdump}` — hexdump of the first 256 bytes, never the raw blob |

**Detection order** ([detect.py](filebridge_mcp/detect.py)): video/audio/office by
**extension** → **magic bytes** (PNG/JPEG/GIF/BMP/PDF) → `PK` zip (office vs
archive) → image/archive extension → UTF-8 **sniff** → `binary`. Magic bytes win
over a misleading extension, since the server crawls a real, possibly mislabeled
folder.

**Image-returning tools** (vision channel) — every one emits a typed
`ImageContent` carrying `_meta` for identity (see [Image identity](#image-identity-_meta)):

| tool | returns (`output="inline"`, the default) | with `output="file"` |
|------|------------------------------------------|----------------------|
| `read_file` (image / `render_page`) | one **Image** + `_meta` | — (image already on disk) |
| `video_frame` | one **Image**, `_meta={path, timestamp_sec}` | JSON `{path, timestamp_sec, frame_path, format}` |
| `video_frames` | **list**: summary JSON + N **Images**, each `_meta={path, timestamp_sec, frame_index}`; a failed frame ⇒ error JSON with the same `frame_index` | JSON `{path, format, frames:[{timestamp_sec, frame_path}]}` |
| `video_contact_sheet` | one tiled **Image**, `_meta={path, kind:"contact_sheet", frame_count, cols}` | JSON `{path, frame_count, cols, sheet_path, format}` |

**Text-only tools** (JSON, straight into context) — never reach the vision channel:

| tool | returns |
|------|---------|
| `list_dir` | `{path, entries:[{path, type, size, mtime}], truncated}` (`type` = `dir` or a detected kind) |
| `stat` | `{path, exists, is_dir, kind, mime, size, mtime}` (+ `duration_sec/width/height/codec` for media when ffmpeg present) |
| `read_bytes` | `{path, offset, length, total_size, hexdump}` |
| `glob` | `{pattern, count, truncated, paths}` |
| `grep` | `{pattern, count, truncated, matches:[{path, line, text}]}` (text files only) |
| `video_info` | `{path, duration_sec, width, height, fps, codec, has_audio}` |
| `write_file` | `{path, mode, bytes_written}` |
| `make_dir` | `{path, created}` |
| `move` | `{src, dst}` |
| `delete` | `{path, deleted}` |

### Image resolution & size

The longest edge of a returned image is governed by **two optional caps**, and the
**effective cap is the smaller of whichever are set — or none at all**:

| model passes `max_dimension`? | `--max-image-dimension` set? | effective cap |
|---|---|---|
| no | no | **none → native resolution** |
| yes | no | the model's value |
| no | yes | the CLI value |
| yes | yes | **`min`** of the two |

- **`max_dimension`** (per call, on every image-returning tool, range `64–8192`) — the
  model's cap. Omit for none.
- **`--max-image-dimension PX`** (operator) — a server-side cap that overrides any
  larger per-call request; unset means no server cap. Use it to bound vision-token /
  VRAM cost regardless of what the model asks:

```bash
uv run filebridge-mcp --root /path --max-image-dimension 512   # nothing exceeds 512px, ever
```

*How a cap is enforced differs by kind:* still images downscale via Pillow's
`thumbnail` (shrink-only, aspect preserved); video frames scale via ffmpeg
(`scale='min(cap,iw)':-2`, which caps **width** and keeps aspect); a rendered **PDF
page** and the **contact sheet** have no native pixel size, so when *no* cap is set
they fall back to a 1024-px render target rather than "native". Without Pillow, still
images are returned at native resolution (the cap is ignored).

Separately, **`format` / `quality`** control the encoded **byte** size — `png`
(lossless) vs `jpeg` + `quality` 1–100. These apply to **every** image-returning
tool (`read_file` images and PDF-page renders, plus the video frame tools). Like the
resolution cap, they resolve **model per-call → server default → built-in**:

| layer | how to set | precedence |
|---|---|---|
| per-call | `format` / `quality` tool args | **wins** when given |
| server default | `--image-format {png,jpeg}` / `--image-quality 1-100` | used when the model omits |
| built-in | — | native for `read_file` images, png for frames, jpeg for contact sheets; quality 85 |

```bash
uv run filebridge-mcp --root /path --image-format jpeg --image-quality 70   # default everything to jpeg@70
```

So `--image-format jpeg` makes the server return JPEG by default everywhere, and a
model that needs lossless can still pass `format="png"` on a specific call (and
likewise raise/lower `quality`). JPEG re-encoding of `read_file` images needs Pillow;
without it those images fall back to native format.

### Video options

The frame tools share these knobs:

- **Seek** — `video_frame` takes `timestamp` (seconds) **or** `percent` (e.g. `percent=60` → the 60% mark). `video_frames` takes either an even `start`/`end`/`count` slice **or** an explicit `timestamps=[…]` list.
- **Encoding** — `format='png'` (lossless) or `format='jpeg'` with `quality` 1–100. JPEG frames are a fraction of the base64 size.
- **`output='inline' | 'file'`** — `inline` returns image content (default). **`file`** writes the frame(s) to the frames dir (`<root>/.filebridge_frames/`, override with `--frames-dir`) and returns *paths* instead of base64. This is the **mtmd fallback** (see below): a host that can't route inline images into the vision encoder can attach the saved file as a real image. It is the only path the read-only server writes to.
- **`video_contact_sheet`** — tiles `count` evenly-spaced, timestamp-labeled frames into a single image (`cols` wide). One composite costs far fewer vision tokens than N separate frames; ideal for a first-pass overview. Needs Pillow.

### Image identity (`_meta`)

**Every** image block the server emits — `read_file` on an image, a rendered PDF page (`render_page=true`), and all the video frame tools — carries `_meta` with at least `{path, kind}`, plus `timestamp_sec`/`frame_index` for video. Because MCP tool results are an ordered array of *typed* blocks, the host isolates images with `[b for b in result.content if b.type == "image"]` and identifies each from its `_meta` — no dependence on block order, and a frame that fails to extract emits a JSON error block tagged with the same index. Text blocks (JSON metadata) flow into the context unchanged.

### The mtmd image seam (why `output='file'` exists)

An MCP tool result carries an image as base64. Whether that base64 becomes *pixels for the vision encoder* or *inert text in the context window* is a host-side decision the server can't force (design §9). If inline images don't reach the model on your host, switch the frame/sheet tools to `output='file'` and have your frontend/backend attach the returned path as a real image on the next turn.

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
including `list_dir`, `glob`, and `grep` results reached through symlinks — is fully
resolved and re-checked against the root before use, and directory walks never
descend into a symlinked directory (no escape, no cycles).

On Windows, reserved device names like (`CON`, `PRN`, `AUX`, `NUL`, `COM1`-`COM9`
, `LPT1`-`LPT9`) are rejected since they denote devices rather than files in the 
root. 

The server is **read-only by default**; writing and deleting require explicit 
`--allow-write` / `--allow-delete`at launch, and the tools are not registered 
otherwise. Even so, an enabled root grants full read/write **within** it: point 
it only at a folder you are willing to expose.

Resource guards keep a confused or adversarial model from stalling the server:
every `ffmpeg`/`ffprobe` call is bounded by `--ffmpeg-timeout` (default 60 s; a
timeout returns an error asking the model to slow down), `grep` scans only the head
of each line and gives up after a few seconds against catastrophic-backtracking
patterns, and `output="file"` video frames are aged out by `--frames-ttl`
(default 1 h). Error messages are scrubbed to root-relative paths so they don't
disclose the server's filesystem layout.

HTTP mode binds `127.0.0.1` by default; `--host 0.0.0.0` opens it to the network.
Authentication is **off unless you set `--auth-token`** (a shared-secret bearer
token) — without it, anyone who can reach the port can drive the tools, so only use
unauthenticated HTTP on a trusted network or behind a proxy that adds auth. Note
that HTTP mode does **not** enable DNS-rebinding / `Host`-header protection (left off
deliberately so local `http://127.0.0.1` testing isn't blocked), so a malicious web
page could reach a `127.0.0.1` instance; bind to a non-loopback host only behind
auth. Treat file *contents* as untrusted input to the model (prompt-injection risk).

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
