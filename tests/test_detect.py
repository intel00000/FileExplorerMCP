"""Type-detection tests (design D1).

Imports only filebridge_mcp.detect / .config — no MCP SDK required.
"""

from __future__ import annotations

from filebridge_mcp.detect import detect_kind, hexdump, looks_text

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 16
PDF_MAGIC = b"%PDF-1.7\n" + b"binary junk \x00\x01"
ZIP_MAGIC = b"PK\x03\x04" + b"\x00" * 16


def test_looks_text_true_for_utf8():
    assert looks_text("hello, wörld\n".encode("utf-8"))


def test_looks_text_false_for_nul():
    assert not looks_text(b"abc\x00def")


def test_text_detected_by_content(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("just some text\nmore text\n", encoding="utf-8")
    kind, _ = detect_kind(f)
    assert kind == "text"


def test_png_detected_by_magic_over_extension(tmp_path):
    # Wrong extension on purpose — magic bytes must win.
    f = tmp_path / "image.dat"
    f.write_bytes(PNG_MAGIC)
    kind, mime = detect_kind(f)
    assert kind == "image"
    assert mime == "image/png"


def test_jpeg_detected_by_magic(tmp_path):
    f = tmp_path / "photo.bin"
    f.write_bytes(JPEG_MAGIC)
    assert detect_kind(f)[0] == "image"


def test_pdf_detected_by_magic(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(PDF_MAGIC)
    kind, mime = detect_kind(f)
    assert kind == "pdf"
    assert mime == "application/pdf"


def test_video_detected_by_extension(tmp_path):
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    assert detect_kind(f)[0] == "video"


def test_office_zip_vs_archive(tmp_path):
    docx = tmp_path / "report.docx"
    docx.write_bytes(ZIP_MAGIC)
    assert detect_kind(docx)[0] == "office"

    zipf = tmp_path / "bundle.zip"
    zipf.write_bytes(ZIP_MAGIC)
    assert detect_kind(zipf)[0] == "archive"


def test_binary_fallthrough(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(b"\x00\x01\x02\x03\xff\xfe")
    assert detect_kind(f)[0] == "binary"


def test_hexdump_format():
    dump = hexdump(b"ABC\x00\xff")
    assert dump.startswith("00000000  ")
    assert "41 42 43 00 ff" in dump
    # Non-printable bytes render as '.'; printable ASCII stays.
    assert dump.rstrip().endswith("ABC..")
