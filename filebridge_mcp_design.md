# filebridge_mcp — Design Document & Interface Specification

**Status:** Draft for review
**Last updated:** 2026-06-24
**Component:** standalone MCP server (`filebridge_mcp/` package)

---

## 1. Summary

`filebridge_mcp` is a standalone [Model Context Protocol](https://modelcontextprotocol.io) server that exposes a single host folder — Windows or Linux — to a vision-language model (VLM) as an FTP-like surface: explore, read, write, search, plus timestamp-addressed video frame extraction.

The defining constraint shapes the entire design: **a VLM can only consume two things — text and images.** It cannot do anything with the raw bytes of a PDF, a JPEG, or an MP4. So the server is not a byte pipe. Its central operation, `read_file`, is a *renderer* that detects each file's type and projects it into one of the two channels the model can actually use. Everything else (sandboxing, slicing, the verb surface) follows from that.

The server is transport-agnostic (stdio or streamable HTTP) and host-agnostic: it runs identically under llama-server's built-in MCP client, Claude Desktop, Continue, or any other MCP host. It depends only on the MCP SDK for its core; PDF, image-downscaling, and video features degrade gracefully when their optional dependencies are absent.

---

## 2. Background & Motivation

The goal is a VLM that can self-navigate a real folder of mixed content — text, code, documents, images, and especially video — reading and writing files on demand, the way an FTP client roams a directory tree. The naive framing is "give the model a tool that returns file bytes." That framing fails immediately: an FTP client streams opaque bytes and lets a human or program decide what to do with them, but here the model *is* the client, and a model handed the bytes of a JPEG or a PDF has no way to interpret them.

The correct abstraction is therefore not "return bytes" but "project the file into text or an image." A `read` call must dispatch on detected file type and return text for text-like files, an image (reaching the vision encoder) for images and rendered pages, and structured metadata plus on-demand frames for video. This reframing is the core design decision; the rest of the document elaborates it.

A secondary motivation is portability. llama-server already ships built-in agent tools (`read_file`, `write_file`, `grep_search`, `file_glob_search`, etc.) that cover the *text* half of folder access, but they are llama.cpp-only and, critically, do not return images or extract content from non-text formats. A standalone MCP server supplies exactly the missing capability — vision-channel file access — and, because it speaks MCP, is reusable across every host rather than tied to one runtime.

---

## 3. Goals & Non-Goals

### Goals

- Let a VLM **explore** a folder (list/stat/glob/grep) and **read any file** projected into text or images.
- Let the model **write** back into the folder (create/append text, mkdir, move, delete) — the FTP "store" verbs.
- Provide **slice access** everywhere a file can be large: bounded line/page windows for text and PDFs, byte windows for binaries, and time-sliced frame sampling for video.
- Let the model **see video**: probe a media file, then extract single frames (agentic seeking) or a set of frames across a time slice.
- **Confine** all access under one sandbox root, on either OS, with consistent path semantics.
- Run with a **minimal hard dependency** (the MCP SDK), degrading gracefully when optional libraries or `ffmpeg` are unavailable.

### Non-Goals

- Not a general-purpose FTP/file-sync server or a multi-tenant file service.
- No authentication or access control in HTTP mode (deployment concern — see §7).
- No media transcoding, editing, or re-encoding beyond single-frame extraction.
- No full office-document parsing, archive extraction, or binary file *writing* in this version (these are defined extension points — see §11).
- Context-window management is **out of scope by assumption**: compaction is handled upstream in the host runtime (see §6, decision D4).

---

## 4. Architecture

### 4.1 Components

```
   ┌────────────────┐   MCP (stdio / streamable HTTP)   ┌──────────────────────┐
   │  MCP Host       │ <───────────────────────────────> │  filebridge_mcp       │
   │  (llama-server   │   list_dir / read_file / ...      │  ─ sandbox resolver   │
   │   MCP client,    │                                   │  ─ type dispatcher    │
   │   Claude Desktop,│   tool results: text | image      │  ─ slice readers      │
   │   Continue, ...) │                                   │  ─ ffmpeg frame layer │
   │       │          │                                   └──────────┬───────────┘
   │       ▼          │                                              │ confined to
   │   VLM + mtmd      │                                              ▼
   │  (vision encoder) │                                      ┌───────────────┐
   └────────────────┘                                        │  --root folder │
                                                              └───────────────┘
```

The server is a thin, stateless process. It holds no session state; each tool call resolves a path under the root, performs one operation, and returns. The host owns the agent loop (deciding which tool to call and feeding results back into the model). The model's vision encoder (`mtmd`, in the llama.cpp case) is what turns returned image content into embeddings.

### 4.2 Key placement decision — standalone, not native

This capability could have been built as a native C++ tool inside a llama.cpp fork, alongside the existing `--tools` set. That path keeps everything in one binary and, importantly, would let us hand frame bytes straight to `mtmd` and place the media marker ourselves, fully controlling the image pipeline. The cost is that it is llama.cpp-only.

We chose **standalone MCP** for versatility: the same server works across all hosts, and it stays cleanly decoupled from runtime internals. The accepted tradeoff is that we no longer control how a returned image reaches the vision encoder — that now depends on the host's agent loop. This is the project's principal integration risk and is treated explicitly in §9.

### 4.3 Transports

stdio is the default (the standard local-MCP transport; how hosts launch the server as a subprocess). Streamable HTTP (`--http --host H --port N`) is available for remote or multi-client access; it binds `127.0.0.1` by default (local only), with `--host 0.0.0.0` to accept remote connections. Transport selection does not affect tool behavior.

---

## 5. Interface Specification

### 5.1 Conventions

**Paths.** All paths are relative to the root. Input is always treated as relative: leading separators are stripped, the path is fully resolved (following symlinks), and containment under the root is re-verified. Paths are presented back to the model in POSIX form (`movies/2024/clip.mp4`) regardless of host OS, so model behavior does not change between Windows and Linux.

**Response envelope.** Tools return either a JSON string (for metadata and text) or image content (for images and frames). JSON responses are pretty-printed objects. Errors are returned as JSON `{"error": "...message..."}` with an actionable message rather than raised, so the model can read and react to them.

**Image identity.** Every image block — `read_file` on an image, a rendered PDF page, and the video frame tools — is returned as a typed `ImageContent` with `_meta` stamped (`{path, kind}` at minimum, plus `timestamp_sec`/`frame_index` for video). Since a tool result is an ordered array of typed blocks, the host isolates images by type and identifies each from its `_meta` without relying on position; a frame that fails to extract emits an error block carrying the same `frame_index`. (Multi-image returns also include a leading JSON summary for convenience.)

**Slice semantics.** `offset`/`limit` are *generic pagination* whose unit depends on file kind: **lines** for text, **pages** for PDF. Text and PDF responses include a `next_offset` (null when exhausted) so the model can page forward. Byte-level slicing of arbitrary files is a separate tool (`read_bytes`). Video slicing is by **time** (`video_frames`).

**Bounds.** Every potentially large result is capped, with the cap exposed as a parameter and a `truncated`/`next_offset` signal in the response. Defaults: text 400 lines (max 5000), directory 500 entries (max 2000), glob 500 paths, grep 200 matches, frames 8 (max 64). Image/frame longest edge is **uncapped by default** (native resolution); the effective cap is the smaller of the per-call `max_dimension` (≤ 8192) and the operator's `--max-image-dimension`, or none if neither is set.

### 5.2 Type detection

Detection runs magic-byte sniffing first, then file extension, then a UTF-8 sniff of the first 4 KB (a NUL byte forces "binary"). The resulting **kind → channel** mapping is the contract that governs `read_file`:

| kind      | detected by                          | `read_file` projects to                                  |
|-----------|--------------------------------------|----------------------------------------------------------|
| `text`    | UTF-8 sniff / text MIME              | line slice (text channel)                                |
| `image`   | PNG/JPEG/GIF/BMP magic, image exts   | the image, downscaled (vision channel)                   |
| `pdf`     | `%PDF` magic                         | page-range text, or one rasterized page (vision channel) |
| `office`  | `.docx/.pptx/.xlsx`, `PK` zip magic  | note: extension point (not parsed)                       |
| `archive` | `.zip/.tar/...`, `PK` zip magic      | note: extension point (entries not listed)               |
| `video`   | video extensions                     | metadata only → use `video_frame`/`video_frames`         |
| `audio`   | audio extensions                     | metadata only → (transcription is an extension point)    |
| `binary`  | fallthrough                          | stat + hexdump of first 256 bytes (never the raw blob)   |

### 5.3 Tool catalog

Annotation shorthand: **RO** = read-only, non-destructive, idempotent; **WRITE** = mutating, non-destructive, non-idempotent; **DESTRUCTIVE** = mutating, destructive, non-idempotent.

#### Explore

**`list_dir(path=".", depth=1, max_entries=500)`** — RO
List a directory as a tree for orientation. `depth` 1–5 (1 = immediate children). Returns `{path, entries:[{path, type, size, mtime}], truncated}` where `type` is `dir` or a detected file kind.

**`stat(path)`** — RO
Cheap metadata for one path; call before `read_file` to size up a file. Returns `{path, exists, is_dir, kind, mime, size, mtime}`. For video/audio it additionally probes `{duration_sec, width, height, codec}` when `ffmpeg` is present.

#### Read

**`read_file(path, offset=1, limit=400, max_dimension=null, render_page=false)`** — RO
The type-dispatched reader (see §5.2 mapping). For text, returns `{path, kind:"text", total_lines, offset, returned_lines, next_offset, content}`. For images, returns the (downscaled) image. For PDFs, returns page-range text with `next_offset`, or — with `render_page=true` — the single page at `offset` rasterized to an image. For video/audio/office/archive/binary, returns the channel-appropriate result from the mapping table. `offset`/`limit` are lines for text and pages for PDF; `max_dimension` optionally caps image/render output (omit for native resolution — see §5.1 Bounds); `render_page` applies only to PDFs.

**`read_bytes(path, offset=0, length=256)`** — RO
Hexdump an arbitrary byte slice of any file, for inspecting binary formats. `length` ≤ 4096. Returns `{path, offset, length, total_size, hexdump}`.

#### Search

**`glob(pattern, max_results=500)`** — RO
Find paths by glob (supports `**` recursion), relative to root. Returns `{pattern, count, truncated, paths}`.

**`grep(pattern, path_glob="**/*", max_results=200, ignore_case=false)`** — RO
Search text-file contents for a Python regex; pairs with `read_file`'s line offsets. Returns `{pattern, count, truncated, matches:[{path, line, text}]}`. Binary/non-text files and files larger than ~5 MB are skipped.

#### Mutate

**`write_file(path, content, mode="overwrite")`** — WRITE
Write or append UTF-8 text (parent directories created as needed). `mode` ∈ {`overwrite`, `append`}. Returns `{path, mode, bytes_written}`. Binary writes are an extension point. *(See §12 open question on the overwrite annotation.)*

**`make_dir(path)`** — WRITE
Create a directory including parents. Returns `{path, created}`.

**`move(src, dst)`** — DESTRUCTIVE
Move or rename a file/folder within the sandbox. Returns `{src, dst}`.

**`delete(path, recursive=false)`** — DESTRUCTIVE
Delete a file, or a directory (`recursive=true` required for a non-empty directory). Irreversible. Returns `{path, deleted}`.

#### Video

**`video_info(path)`** — RO
Probe a media file so the model knows the time range it can sample. Returns `{path, duration_sec, width, height, fps, codec, has_audio}`. Call before `video_frames` to pick sensible timestamps.

**`video_frame(path, timestamp=null, percent=null, max_dimension=null, format="png", quality=85, output="inline")`** — RO
Return a single frame as an image. Seek by absolute `timestamp` (seconds) **or** by `percent` of the duration (e.g. `percent=60` → the 60% mark; overrides `timestamp`). Intended for **agentic seeking** — narrow toward a moment by repeated calls (e.g. binary-searching for a title card). Returns an image (`output="inline"`), a JSON `{frame_path}` (`output="file"`), or a JSON error.

**`video_frames(path, start=0, end=null, count=8, timestamps=null, max_dimension=null, format="png", quality=85, output="inline")`** — RO
The **slice / set** view: either evenly sample `count` frames (≤ 64) across `[start, end]`, or grab an explicit `timestamps=[…]` list (overrides the slice). Ordered. With `output="inline"` the first list element is a JSON summary of the timestamps and the rest are images; with `output="file"` a single JSON object lists each frame's saved path. Every inline frame is encoded by the vision model in full, so keep `count` modest.

**`video_contact_sheet(path, count=12, cols=4, start=0, end=null, max_dimension=null, format="jpeg", quality=85, output="inline")`** — RO
Tile `count` evenly-spaced, timestamp-labeled frames into **one** composite image (`cols` wide). A single image costs far fewer vision tokens than `count` separate frames — ideal for a first-pass overview before zooming in with `video_frame`. Requires Pillow. Returns an image, or a saved sheet path with `output="file"`.

**Shared frame options.** `max_dimension` optionally caps the frame's longest edge (omit for native; effective cap = smaller of this and `--max-image-dimension`, or none — §5.1). `format` is `png` (lossless) or `jpeg` (much smaller; `quality` 1–100). `output="file"` materializes frames under the frames dir (`<root>/.filebridge_frames`, override with `--frames-dir`) and returns *paths* instead of base64 — this is the §9 mtmd fallback, exposed as a per-call option. It is the only write the read-only server performs.

### 5.4 Frame extraction mechanics

Frames are pulled with `ffmpeg`, seeking with `-ss` placed *before* `-i` (fast approximate keyframe seek; frame-accurate seeking would decode from the start and is not worth the cost for this use). Frames are scaled in `ffmpeg` (`scale='min(W,iw)':-2`, preserving aspect, even height) and emitted to stdout as PNG, or as MJPEG (`-q:v` derived from `quality`) when `format="jpeg"` — no temp files. The contact sheet extracts PNG tiles and composes/labels them with Pillow. Media metadata and duration come from `ffprobe -of json`.

---

## 6. Key Design Decisions

**D1 — `read_file` is a type-projector, not a byte-streamer.**
The central decision (§2). A VLM consumes only text and images, so reads dispatch on detected type and return one of those channels. Returning raw bytes would be useless to the model for any non-text format. Detection is by magic bytes (not extension alone) because the server crawls a real, possibly mislabeled folder.

**D2 — Standalone MCP server over a native llama.cpp tool.**
Chosen for host-portability and decoupling from runtime internals (§4.2). The accepted cost is losing direct control of the image→encoder path, which becomes a host responsibility and the project's main risk (§9). Rationale for accepting it: the capability is valuable across hosts, and the seam is testable and has a defined fallback.

**D3 — Bounded, sliced reads by default.**
Because the *model*, not a human, decides what to pull, an unbounded read is a foot-gun: the model can request an enormous file on a whim. Every large-capable read returns a bounded window plus a continuation cursor, so the model pages deliberately (often `grep` → read the matching line range) instead of vacuuming whole files. Slicing is generic — lines for text, pages for PDF, time for video.

**D4 — Image resolution is capped independently of context compaction.**
Context-window overflow is handled upstream (the host's compaction layer), so text reads need no byte cap for context reasons — they can return whole files within the line ceiling. But image cost is **not** governed by compaction: a frame's resolution sets how many patches the vision encoder emits, and that encode latency and peak VRAM are paid in full during prefill, *before* compaction can evict anything. Therefore `max_dimension` remains a hard, caller-visible cap on every image and frame. The justification is GPU compute/VRAM, not the context window — a distinction worth stating because it determines which knobs matter.

**D5 — Single-root sandbox with resolve-then-contain.**
All access is confined to `--root`. The resolver strips leading separators (input is always relative), fully resolves the path *following symlinks*, then verifies the resolved path is the root or a descendant. Resolving before checking is what closes the symlink-escape hole: a symlink pointing outside the root resolves outside and fails containment. `..` and absolute paths that escape are rejected by the same check.

**D6 — Graceful dependency degradation.**
The core (explore, text read, binary hexdump, write, search) is standard-library-only and always works. PDF (PyMuPDF), image downscaling (Pillow), and video (`ffmpeg`/`ffprobe`) are optional; when missing, the affected tool returns an actionable error message rather than failing to import. This keeps the server runnable in minimal environments and makes setup incremental.

---

## 7. Security Model

**Threat model.** The model is a semi-trusted, possibly-confused agent that may attempt to read or write outside the intended folder, whether through adversarial inputs, prompt injection from file contents, or simple error. The server's job is to make the sandbox root a hard boundary.

**Guarantees.** Every path passes through the resolve-then-contain check (D5), so no tool can touch anything outside the root, including via symlinks. The server is **read-only by default**: the mutating tools are not registered at all unless the operator opts in at launch — `--allow-write` for `write_file`/`make_dir`, `--allow-delete` for `move`/`delete` (or `--allow-all` for both). An unregistered tool is invisible to the model, a stronger guarantee than trusting the host to honor annotations. When enabled, the mutating tools are still annotated for hosts that gate by hint: `move` and `delete` carry `destructiveHint: true`; `delete` additionally requires an explicit `recursive` flag for non-empty directories.

**HTTP authentication.** HTTP mode is unauthenticated by default. Setting `--auth-token <token>` (or the `FILEBRIDGE_AUTH_TOKEN` env var, preferred so the secret stays out of the process list) registers a `TokenVerifier` that requires a matching `Authorization: Bearer <token>` on every request — a constant-time-compared shared secret, not OAuth; anything else gets `401`. Auth applies to the HTTP transport only (stdio has no headers). Note the server does **not** enable the SDK's DNS-rebinding / `Host`-header protection (`transport_security` is left unset, which disables it), so a malicious web page could reach a `127.0.0.1` instance; bind a non-loopback host only behind `--auth-token` or a fronting proxy.

**Deployment cautions.** When write/delete are enabled the server grants full read/write within the root — point it only at a folder you are willing to fully expose. Do not expose unauthenticated HTTP on an untrusted network. Even with the sandbox, treat file *contents* as untrusted input to the model (injection risk is a host/model concern, not something the server can neutralize).

---

## 8. Error Handling

Errors are data, not exceptions: tools return `{"error": "..."}` with a message that tells the model what to do next (e.g. "Path '../x' escapes the sandbox root…", "PDF support needs PyMuPDF. Run: pip install pymupdf", "ffmpeg not found on PATH. Install FFmpeg…"). Path-escape attempts raise `ValueError` in the resolver, surfaced as an error string. Missing optional dependencies and missing `ffmpeg`/`ffprobe` are reported as actionable errors. Sub-operations that can partially fail (e.g. a single frame in `video_frames`) report the per-item error inline while still returning the successful items.

---

## 9. The mtmd Image Seam (principal risk)

The one assumption that must be validated before relying on this server: **that an MCP tool result containing image content actually reaches the VLM's vision encoder through the host's agent loop.** The server correctly emits image content; whether llama-server's MCP client routes that image into `mtmd` (versus treating it as an inert base64 blob) is a host-side question, and the agent loop and multimodal support in llama.cpp were built largely independently.

**Validation procedure.** Point the host at the server and, as the very first test, call `video_frame` (or `read_file` on an image file). If the model describes the frame, the seam works and the whole design holds. If the model sees only a blob, the fix is host-side, not in this server.

**Fallback if the seam fails (now built in).** The frame tools accept `output="file"`: they write the frame/sheet to disk under the root (the frames dir) and return *paths* instead of base64. The host then attaches those files as `image_url` content on the next turn (the chat endpoint accepts images directly). This is slightly less "agentic" but reliable and needs no server redesign — the tools already operate within the sandbox. (Extending `output="file"` to `read_file` on still images is a small, obvious follow-up.)

---

## 10. Testing & Evaluation

**Static.** `uv run pytest` (sandbox containment + type detection, SDK-free); `uv run filebridge-mcp --help` (or `python -m filebridge_mcp --help`) to confirm imports resolve and all tools register.

**Runtime smoke (validated).** Directory listing with type detection; sliced text reads returning correct `next_offset`; magic-byte image detection; the traversal guard rejecting `../../etc/passwd`; `stat`. Video paths require `ffmpeg` and are exercised separately.

**Interactive.** MCP Inspector (`npx @modelcontextprotocol/inspector`) to drive each tool by hand and inspect schemas.

**Agent evaluation.** A set of ~10 read-only, verifiable tasks that require the model to actually compose these tools over the folder (e.g. "find the file mentioning X and report the value on the line below it", "what is shown at the 60% mark of clip Y") — to confirm a given model can *drive* the surface, not just that the tools work in isolation.

---

## 11. Extension Points & Future Work

- **Office extraction** (`office` kind): parse `.docx/.pptx/.xlsx` to text via python-docx / python-pptx / openpyxl.
- **Archive listing** (`archive` kind): return entry listings; never auto-extract.
- **Binary writes**: accept base64 content in `write_file` (or a dedicated tool) and decode.
- **Audio understanding**: `whisper.cpp` transcription, and a `get_subtitles(path, start, end)` tool that slices sidecar `.srt`/`.vtt` — so the model can "hear" dialogue, not only see frames.
- **Native bulk-frame video**: an alternative to agentic seeking that feeds a frame sequence as a single multi-image input, leveraging M-RoPE temporal position IDs in Qwen2-VL/Qwen3-VL-class models for true video understanding.
- **HTTP hardening**: a path allowlist, rate limiting, and opt-in DNS-rebinding protection (`transport_security`) for multi-client deployment. Bearer-token auth (`--auth-token`) is implemented; full OAuth via `auth_server_provider` remains an option.
- **MCP Resources**: expose static/semi-static files as resources (URI templates) in addition to tools, for hosts that prefer resource-style access.

---

## 12. Open Questions

1. **`write_file` overwrite annotation.** Overwrite replaces existing content and is arguably destructive, yet it is currently annotated non-destructive (the common case is create/append). Should we split create vs. overwrite into separate tools, or annotate `write_file` destructive and accept the friction?
2. **Read via Tools vs. Resources.** For purely static reads, MCP Resources may be a cleaner fit than a tool; worth deciding per target host's capabilities.
3. **Default frame count.** Is 8 the right default for `video_frames`, given downstream context budget and per-frame encode cost? Should the default adapt to clip duration?
4. **Encouraging paging.** Is `next_offset` in the response sufficient to make models page large files, or do we need a more explicit "this file is large; here is a window" affordance?
5. **Symlink policy.** We currently follow symlinks and rely on post-resolution containment. Should we instead reject symlinks outright for a stricter posture?

---

## Appendix A — Dependency Matrix

| Capability                         | Requirement                  | If missing                          |
|------------------------------------|------------------------------|-------------------------------------|
| Core (explore, text, binary, write, search) | `mcp` (Python SDK)   | — (required)                        |
| Image downscaling                  | Pillow                       | image returned at native resolution |
| PDF text & page rendering          | PyMuPDF (`fitz`)             | PDF tools return actionable error   |
| Video info & frame extraction      | `ffmpeg` + `ffprobe` on PATH | video tools return actionable error |

## Appendix B — Configuration

stdio (typical local host config):

```json
{
  "filebridge": {
    "command": "uv",
    "args": ["run", "filebridge-mcp", "--root", "/path/to/folder"]
  }
}
```

(Or `"command": "python", "args": ["-m", "filebridge_mcp", "--root", "/path/to/folder"]`.)

HTTP (remote/multi-client); binds `127.0.0.1` unless `--host` is given. Add a
shared-secret bearer token with `--auth-token` / `FILEBRIDGE_AUTH_TOKEN`:

```bash
uv run filebridge-mcp --root /path/to/folder --http --port 8000              # local only, no auth
uv run filebridge-mcp --root /path/to/folder --http --host 0.0.0.0 --port 8000  # remote, no auth
FILEBRIDGE_AUTH_TOKEN=secret \
  uv run filebridge-mcp --root /path/to/folder --http --host 0.0.0.0 --port 8000  # remote + bearer token
```

The root may also be supplied via the `MCP_ROOT` environment variable; `--root` takes precedence.
