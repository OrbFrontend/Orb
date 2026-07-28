"""The content-addressed package store: ``data/extensions/objects/<digest>/``.

Package files live beside the database, never inside the frontend tree, and
every path in the store is derived from a validated digest plus a normalized
package path. No path a package supplied is ever persisted or joined at request
time -- the asset route resolves a compiled key to a descriptor, and this module
is the only thing that turns that descriptor into a filesystem location.

Durability order matters and is the reason this is not three ``open()`` calls:

1. Write the whole revision into a temporary sibling directory.
2. ``fsync`` every file, then the directory.
3. ``os.replace`` it into its digest-named home, then ``fsync`` the parent.

A crash before step 3 leaves an unreferenced temporary directory that
:func:`collect_garbage` removes. A crash after it leaves complete content that
no database row points at yet -- also collectable. What cannot happen is a
digest directory containing a partially written revision, which is the one
state a reader could not detect: the digest names content the directory does
not contain, and every later check would compare a hash against bytes that
were never all there.

The root is derived from ``connection.DB_PATH`` at call time, not at import,
so a test that monkeypatches the database path gets its own store -- the same
discipline ``features/presets`` uses for snapshots.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import suppress

from ...database import connection
from .digest import PackageContent, content_digest
from .errors import PackageValidationError
from .paths import normalize_package_path

logger = logging.getLogger(__name__)

_DIGEST_LENGTH = 64
_DIGEST_ALPHABET = frozenset("0123456789abcdef")


def store_root() -> str:
    """``<dirname(DB_PATH)>/extensions/objects``, resolved fresh each call."""
    return os.path.join(os.path.dirname(os.path.abspath(connection.DB_PATH)), "extensions", "objects")


def _validate_digest(digest: str) -> str:
    """A digest is a path component, so it is validated like one.

    Length plus alphabet, not a regex over "looks hex enough": this value comes
    back from the database and from request bodies, and it becomes a directory
    name. ``..`` is not hex, but neither is anything else that would be
    interesting here, so the allowlist is the whole check.
    """
    if not isinstance(digest, str) or len(digest) != _DIGEST_LENGTH or not set(digest) <= _DIGEST_ALPHABET:
        raise PackageValidationError("content digest must be 64 lowercase hex characters")
    return digest


def content_path(digest: str) -> str:
    """The directory a revision's files live in. Existence not implied."""
    return os.path.join(store_root(), _validate_digest(digest))


def exists(digest: str) -> bool:
    return os.path.isdir(content_path(digest))


def materialize(files: Mapping[str, PackageContent]) -> str:
    """Write a selected file set durably and return its digest.

    Idempotent: a digest already present is left untouched and returned. That
    is not just an optimization -- two packages can legitimately share a
    revision (a reinstall, a rollback to a digest still on disk), and rewriting
    it would replace live content that an in-flight snapshot still describes.

    The digest is computed here rather than accepted from the caller, so the
    directory name is always a function of the bytes it contains.
    """
    digest = content_digest(files)
    target = content_path(digest)
    if os.path.isdir(target):
        return digest

    root = store_root()
    os.makedirs(root, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".staging-", dir=root)
    try:
        for raw_path, content in files.items():
            path = normalize_package_path(raw_path, what="package file path")
            destination = os.path.join(staging, *path.split("/"))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as fh:
                fh.write(content.canonical_bytes())
                fh.flush()
                os.fsync(fh.fileno())
        _fsync_tree(staging)
        try:
            os.replace(staging, target)
        except OSError:
            # A concurrent materialize of the same digest won the race (on
            # Windows os.replace onto an existing directory fails). Identical
            # content by construction, so the winner's copy is correct.
            if not os.path.isdir(target):
                raise
            shutil.rmtree(staging, ignore_errors=True)
            return digest
        _fsync_dir(root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return digest


def remove(digest: str) -> None:
    """Delete one revision's content. Missing is success, not an error."""
    shutil.rmtree(content_path(digest), ignore_errors=True)


def usage() -> tuple[int, int]:
    """``(revision count, bytes on disk)`` for the whole store.

    Observability only -- nothing here decides what to collect. Staging
    leftovers count toward the bytes but not the revisions: they occupy the
    disk the user is being shown, and a crash mid-install is exactly when that
    number is worth seeing, but they are not revisions anything can name.
    """
    root = store_root()
    if not os.path.isdir(root):
        return 0, 0
    revisions = total = 0
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if not name.startswith(".staging-"):
            revisions += 1
        for dirpath, _dirnames, filenames in os.walk(path):
            for filename in filenames:
                # A file collected out from under the walk is not an error to
                # report; it is a smaller number, which is the honest answer.
                with suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, filename))
    return revisions, total


def collect_garbage(keep: Iterable[str]) -> list[str]:
    """Remove every stored revision not in *keep*, and every staging leftover.

    *keep* must already include the digests pinned by live runtime snapshots as
    well as those the database references -- an in-flight invocation holds
    compiled objects, but the asset route still resolves against the directory,
    so collecting a digest a snapshot names would break requests that are
    already valid.

    Returns the digests removed, for the startup log.
    """
    root = store_root()
    if not os.path.isdir(root):
        return []
    retained = set(keep)
    removed: list[str] = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if name.startswith(".staging-"):
            shutil.rmtree(path, ignore_errors=True)
            continue
        if len(name) != _DIGEST_LENGTH or not set(name) <= _DIGEST_ALPHABET:
            # Not something this module wrote; leave it rather than delete an
            # unknown directory under the user's data folder.
            logger.warning("extension content store: ignoring unexpected entry %r", name)
            continue
        if name not in retained:
            remove(name)
            removed.append(name)
    return removed


def _fsync_tree(root: str) -> None:
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        _fsync_dir(dirpath)


def _fsync_dir(path: str) -> None:
    """fsync a directory so the rename/creation itself is durable.

    Not supported on Windows, where directory handles cannot be opened for
    sync; the ``os.replace`` there is still atomic, which is the property the
    reader depends on. Swallowing the error keeps one platform's missing
    primitive from turning every install into a failure.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:  # pragma: no cover -- platform dependent
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover -- platform dependent
        pass
    finally:
        os.close(fd)
