"""Sandbox containment tests — the security core (design D5).

Imports only filebridge_mcp.sandbox, which has no MCP-SDK dependency, so these run
in a minimal environment.
"""

from __future__ import annotations

import pytest

from filebridge_mcp.sandbox import Root


def test_resolve_normal_path(tmp_path):
    root = Root(tmp_path)
    (tmp_path / "sub").mkdir()
    target = root.resolve("sub/file.txt")
    assert target == (tmp_path / "sub" / "file.txt").resolve()


def test_leading_separators_treated_relative(tmp_path):
    root = Root(tmp_path)
    # A leading slash must not anchor to the filesystem root.
    assert root.resolve("/a/b") == (tmp_path / "a" / "b").resolve()


def test_backslashes_normalized(tmp_path):
    root = Root(tmp_path)
    assert root.resolve("a\\b\\c") == (tmp_path / "a" / "b" / "c").resolve()


def test_dotdot_escape_rejected(tmp_path):
    (tmp_path / "inner").mkdir()
    root = Root(tmp_path / "inner")
    with pytest.raises(ValueError):
        root.resolve("../secret.txt")


def test_deep_dotdot_escape_rejected(tmp_path):
    (tmp_path / "inner").mkdir()
    root = Root(tmp_path / "inner")
    with pytest.raises(ValueError):
        root.resolve("a/b/../../../escape")


def test_absolute_path_never_escapes(tmp_path):
    root = Root(tmp_path)
    other = tmp_path.parent / "outside.txt"
    # The security property, stated platform-agnostically: an OS-absolute path is
    # either rejected (Windows drive-absolute resets the drive on join, failing
    # containment) or re-rooted inside (POSIX leading-slash is stripped). It must
    # never silently resolve OUTSIDE the root.
    try:
        resolved = root.resolve(str(other))
    except ValueError:
        return  # rejected — safe
    assert resolved.is_relative_to(tmp_path.resolve())  # re-rooted inside — safe


def test_root_itself_is_within(tmp_path):
    root = Root(tmp_path)
    assert root.is_within(tmp_path)
    assert root.resolve(".") == tmp_path.resolve()


def test_sibling_not_within(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    root = Root(tmp_path / "a")
    assert not root.is_within(tmp_path / "b")


def test_rel_roundtrip(tmp_path):
    root = Root(tmp_path)
    p = root.resolve("x/y.txt")
    assert root.rel(p) == "x/y.txt"
    assert root.rel(tmp_path) == "."


def test_symlink_escape_rejected(tmp_path):
    """A symlink pointing outside the root must fail containment (resolve-then-contain)."""
    (tmp_path / "root").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    link = tmp_path / "root" / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    root = Root(tmp_path / "root")
    # Following the link resolves outside the root → rejected.
    with pytest.raises(ValueError):
        root.resolve("escape/secret.txt")
    assert not root.is_within(link / "secret.txt")


def test_reserved_device_names_rejected_on_windows(tmp_path, monkeypatch):
    """On Windows, reserved device names (CON/NUL/COM1...) must be rejected even
    though they sit lexically under the root."""
    from filebridge_mcp import sandbox

    monkeypatch.setattr(sandbox, "_is_windows", lambda: True)
    root = sandbox.Root(tmp_path)
    for name in ["CON", "nul", "COM1", "LPT9", "AUX", "CON.txt", "sub/PRN", "con "]:
        with pytest.raises(ValueError):
            root.resolve(name)
    # A normal name is unaffected.
    assert root.resolve("notes.txt") == (tmp_path / "notes.txt").resolve()


def test_reserved_device_names_allowed_off_windows(tmp_path, monkeypatch):
    """On POSIX, NUL/CON are legal filenames and must stay accessible."""
    from filebridge_mcp import sandbox

    monkeypatch.setattr(sandbox, "_is_windows", lambda: False)
    root = sandbox.Root(tmp_path)
    assert root.resolve("NUL") == (tmp_path / "NUL").resolve()
    assert root.resolve("COM1.log") == (tmp_path / "COM1.log").resolve()


def test_scrub_redacts_absolute_root_prefix(tmp_path):
    root = Root(tmp_path)
    p = root.resolve("sub/clip.mp4")
    msg = f"ffmpeg failed on {p}: bad data"
    scrubbed = root.scrub(msg)
    assert str(tmp_path) not in scrubbed
    assert "sub/clip.mp4" in scrubbed.replace("\\", "/")
