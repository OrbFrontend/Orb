"""The ``.orbext`` reader: structure, limits, and what it refuses to expand.

The reader is the first thing hostile bytes reach. Its contract is narrow on
purpose -- ask whether a normalized path exists, read at most N bytes of it --
so these tests are mostly about what it rejects before any of that.
"""

from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest

from backend.features.extensions.errors import (
    PackageLimitExceeded,
    PackageValidationError,
)
from backend.features.extensions.limits import MAX_TREE_ENTRIES
from backend.features.extensions.sources import MANIFEST_PATH, ArchiveSource
from tests.extension_packages import manifest, metadata_package, orbext


def _zip(entries: list[tuple[str, bytes, int]]) -> bytes:
    """Build an archive with explicit unix modes, which ``writestr`` cannot set."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def test_reads_a_referenced_file_and_nothing_else():
    data = orbext({"orb-extension.json": manifest(), "README.md": b"hello"})
    with ArchiveSource(data) as source:
        assert source.has(MANIFEST_PATH)
        assert json.loads(source.read(MANIFEST_PATH, max_bytes=4096))["id"] == "scene-meter"
    # There is no listing call: the only way to reach README.md is to name it,
    # which is what makes "unreferenced files are never compiled" structural.
    assert not hasattr(ArchiveSource, "list")


def test_strips_a_single_wrapping_directory():
    """``zip -r pkg.orbext my-extension/`` is the command people actually run."""
    with ArchiveSource(orbext({"orb-extension.json": manifest()}, root="my-extension")) as source:
        assert source.has(MANIFEST_PATH)


def test_does_not_guess_a_root_when_several_top_level_entries_exist():
    data = orbext({"a/orb-extension.json": manifest(), "b/notes.txt": b"x"})
    with pytest.raises(PackageValidationError, match="does not contain orb-extension.json"):
        ArchiveSource(data)


def test_rejects_a_symlink_entry():
    """A symlink is how a package escapes the content store on extraction."""
    data = _zip(
        [
            ("orb-extension.json", json.dumps(manifest()).encode(), 0o100644),
            ("ui/evil.json", b"/etc/passwd", stat.S_IFLNK | 0o777),
        ]
    )
    with pytest.raises(PackageValidationError, match="symlink"):
        ArchiveSource(data)


@pytest.mark.parametrize(
    "name",
    ["../escape.json", "/absolute.json", "ui/../../escape.json", "ui/./view.json", "C:/drive.json"],
)
def test_rejects_paths_that_are_not_contained_relative_paths(name: str):
    data = _zip([("orb-extension.json", json.dumps(manifest()).encode(), 0o100644), (name, b"{}", 0o100644)])
    with pytest.raises(PackageValidationError):
        ArchiveSource(data)


def test_rejects_paths_that_collide_when_case_is_folded():
    """Two entries the host filesystem cannot store distinctly are one package
    on macOS and two on Linux -- so the digest would describe content this
    machine cannot reproduce."""
    data = orbext({"orb-extension.json": manifest(), "ui/View.json": b"{}", "ui/view.json": b"{}"})
    with pytest.raises(PackageValidationError, match="collide"):
        ArchiveSource(data)


def test_rejects_an_archive_with_too_many_entries():
    files = {"orb-extension.json": manifest()}
    files.update({f"pad/{i}.json": b"{}" for i in range(MAX_TREE_ENTRIES + 1)})
    with pytest.raises(PackageLimitExceeded, match="entries"):
        ArchiveSource(orbext(files))


def test_rejects_a_file_that_expands_past_its_read_limit():
    """The declared size is a hint for rejecting early, never the authority."""
    with ArchiveSource(orbext({"orb-extension.json": manifest(), "big.json": b"x" * 5000})) as source:
        with pytest.raises(PackageLimitExceeded):
            source.read("big.json", max_bytes=100)


def test_rejects_a_missing_manifest():
    with pytest.raises(PackageValidationError, match="orb-extension.json"):
        ArchiveSource(orbext({"flows/a.json": b"{}"}))


def test_rejects_bytes_that_are_not_a_zip():
    from backend.features.extensions.errors import PackageParseError

    with pytest.raises(PackageParseError):
        ArchiveSource(b"not a zip at all")


def test_reading_an_unlisted_path_is_an_error_not_an_empty_result():
    with ArchiveSource(metadata_package()) as source:
        assert not source.has("flows/missing.json")
        with pytest.raises(PackageValidationError, match="does not contain"):
            source.read("flows/missing.json", max_bytes=1024)
