"""Bounded readers: the two ways package bytes reach the compiler.

Both sources answer the same two questions -- "does this normalized path
exist?" and "give me at most N bytes of it" -- and neither lets the compiler
enumerate, glob, or walk. That shape is what makes "unreferenced repository
files are never compiled, persisted, or served" a property of the *interface*
rather than a discipline the compiler has to remember: there is no call that
returns a file the manifest did not name.

:class:`ArchiveSource` reads a ``.orbext`` ZIP the user uploaded.
:class:`StoredSource` reads a digest-owned directory in the content store. The
compiler cannot tell them apart, which is what lets install and startup
reconciliation run the *same* compilation over the same bytes and get the same
digest -- if inspection and startup could disagree, a package would activate as
something other than what the user consented to.
"""

from __future__ import annotations

import os
import posixpath
import stat
import zipfile
from typing import Protocol

from .errors import PackageLimitExceeded, PackageParseError, PackageValidationError
from .limits import MAX_REFERENCED_BYTES_TOTAL, MAX_SOURCE_BYTES, MAX_TREE_ENTRIES
from .paths import assert_no_case_collisions, normalize_package_path

MANIFEST_PATH = "orb-extension.json"


class PackageSource(Protocol):
    """A read-only, path-keyed view of one package's files."""

    def has(self, path: str) -> bool: ...

    def read(self, path: str, *, max_bytes: int) -> bytes: ...


class _BudgetedSource:
    """Shared expanded-byte accounting.

    The per-file limit and the aggregate limit are different guarantees: 40
    referenced files of 1 MiB each are individually fine and collectively past
    the package budget. Charging the budget in the base class means a source
    cannot implement ``read`` and forget the running total.
    """

    def __init__(self) -> None:
        self._spent = 0

    def _charge(self, path: str, size: int) -> None:
        self._spent += size
        if self._spent > MAX_REFERENCED_BYTES_TOTAL:
            raise PackageLimitExceeded(
                f"referenced package files exceed the total limit of {MAX_REFERENCED_BYTES_TOTAL} bytes "
                f"(while reading {path!r})"
            )


class ArchiveSource(_BudgetedSource):
    """A ``.orbext`` ZIP, indexed but not extracted.

    The constructor walks the central directory and rejects the whole archive
    on any structural violation; it never writes a file and never decompresses
    an entry. Bytes are produced only by :meth:`read`, only for a path the
    compiler asks for by name, and only through a bounded stream -- the
    declared ``file_size`` is treated as a hint to reject early, never as the
    authority on how much was actually written.
    """

    def __init__(self, data: bytes) -> None:
        super().__init__()
        if len(data) > MAX_SOURCE_BYTES:
            raise PackageLimitExceeded(f"archive is {len(data)} bytes, limit is {MAX_SOURCE_BYTES}")
        try:
            self._zip = zipfile.ZipFile(_BytesReader(data))
        except zipfile.BadZipFile as exc:
            raise PackageParseError(f"not a readable .orbext archive ({exc})") from None
        self._entries = _index_archive(self._zip)

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> ArchiveSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def has(self, path: str) -> bool:
        return path in self._entries

    def read(self, path: str, *, max_bytes: int) -> bytes:
        info = self._entries.get(path)
        if info is None:
            raise PackageValidationError(f"package does not contain {path!r}")
        if info.file_size > max_bytes:
            raise PackageLimitExceeded(f"{path}: declared {info.file_size} bytes, limit is {max_bytes}")
        with self._zip.open(info) as fh:
            # One byte past the limit distinguishes "exactly at the limit" from
            # "the header lied and there is more coming".
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise PackageLimitExceeded(f"{path}: expands past the limit of {max_bytes} bytes")
        self._charge(path, len(data))
        return data


class StoredSource(_BudgetedSource):
    """A materialized content directory, read by exact compiled key.

    ``root`` is derived from a validated digest by the content store; the path
    handed here is a normalized package path re-joined onto it after a second
    containment check. The realpath assertion is not redundant with
    :func:`normalize_package_path` -- normalization proves the *string* cannot
    traverse, and this proves the *filesystem* did not (a symlink planted in the
    store by something other than Orb).
    """

    def __init__(self, root: str) -> None:
        super().__init__()
        self._root = os.path.realpath(root)

    def _resolve(self, path: str) -> str:
        normalized = normalize_package_path(path, what="package path")
        candidate = os.path.realpath(os.path.join(self._root, *normalized.split("/")))
        if candidate != self._root and not candidate.startswith(self._root + os.sep):
            raise PackageValidationError(f"package path {path!r} resolves outside the content directory")
        return candidate

    def has(self, path: str) -> bool:
        try:
            return os.path.isfile(self._resolve(path))
        except PackageValidationError:
            return False

    def read(self, path: str, *, max_bytes: int) -> bytes:
        resolved = self._resolve(path)
        if not os.path.isfile(resolved):
            raise PackageValidationError(f"package content is missing {path!r}")
        with open(resolved, "rb") as fh:
            data = fh.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise PackageLimitExceeded(f"{path}: exceeds the limit of {max_bytes} bytes")
        self._charge(path, len(data))
        return data


class _BytesReader:
    """Minimal seekable file object over a bytes buffer.

    ``zipfile`` wants a file-like; ``io.BytesIO`` would work, but this keeps the
    uploaded bytes from being copied into a second buffer the archive limit
    already accounted for.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self._data) if size is None or size < 0 else min(len(self._data), self._pos + size)
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self._pos, os.SEEK_END: len(self._data)}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def seekable(self) -> bool:
        return True


def _index_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Validate the central directory and return normalized path -> entry.

    Everything rejected here is rejected for the *whole* archive rather than by
    skipping the offending entry. A package that ships a symlink or a traversal
    path is not a package with one bad file; it is a package doing something
    the format does not permit, and silently dropping the entry would let the
    author believe their layout worked.

    An optional single top-level directory is stripped: ``zip -r pkg.orbext
    my-extension/`` is the shape every archiving tool produces, and requiring
    the manifest at the archive root would make the common command wrong.
    """
    infos = archive.infolist()
    if len(infos) > MAX_TREE_ENTRIES:
        raise PackageLimitExceeded(f"archive has {len(infos)} entries, limit is {MAX_TREE_ENTRIES}")

    raw: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        # Unix mode lives in the high half of external_attr, and only when the
        # archive was written by a Unix tool. A DOS/Windows writer leaves it
        # zero, and Python's own ``writestr`` stores permission bits with no
        # file-type bits at all -- so the check is on the *type* field when one
        # is present, not on the whole mode. Treating an absent type as "not a
        # regular file" would reject archives produced by half the world's zip
        # implementations; treating a present S_IFLNK as fine would let a
        # package ship a symlink out of the content store.
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind == stat.S_IFLNK:
            raise PackageValidationError(f"archive entry {name!r} is a symlink; packages contain regular files only")
        if kind not in (0, stat.S_IFREG):
            raise PackageValidationError(f"archive entry {name!r} is not a regular file")
        path = normalize_package_path(name, what="archive entry")
        if path in raw:
            raise PackageValidationError(f"archive contains duplicate entry {path!r}")
        raw[path] = info

    if not raw:
        raise PackageValidationError("archive contains no files")

    entries = _strip_common_root(raw)
    assert_no_case_collisions(entries)
    if MANIFEST_PATH not in entries:
        raise PackageValidationError(f"archive does not contain {MANIFEST_PATH}")
    return entries


def _strip_common_root(entries: dict[str, zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
    """Drop one shared leading directory, but only when it is unambiguous.

    Conditions: the manifest is not already at the root, and every entry shares
    exactly one first segment. Anything else keeps the paths as-is -- guessing a
    root from a partial prefix would change which file a manifest reference
    resolves to based on which unrelated files happened to be in the archive.
    """
    if MANIFEST_PATH in entries:
        return entries
    roots = {path.split("/", 1)[0] for path in entries}
    if len(roots) != 1:
        return entries
    root = next(iter(roots))
    stripped = {posixpath.relpath(path, root): info for path, info in entries.items()}
    return stripped if MANIFEST_PATH in stripped else entries
