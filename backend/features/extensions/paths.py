"""Package-path normalization: the only place a package string becomes a path.

A package path is a *key*, not a filesystem location. It is normalized and
validated here, then used to look up a compiled entry; it is never joined onto
a directory at request time. That is the property the asset route depends on --
"resolve an exact compiled asset key to a digest-owned descriptor; never join
the request path onto a directory".

The rules are deliberately stricter than any one platform's:

* relative, forward-slash separated, no leading ``/`` and no drive letter
* no ``.`` or ``..`` segment, no empty segment, no trailing slash
* no NUL, no backslash, no control characters, no whitespace-only segment
* NFC-normalized and case-sensitive, but **collision-checked case-folded**, so
  a package cannot ship ``ui/View.json`` and ``ui/view.json`` and get different
  content depending on whether the host filesystem folds case
* at most :data:`MAX_PATH_BYTES` UTF-8 bytes

Backslash is rejected rather than translated: on Windows ``a\\b`` names a
nested file, on Linux it names a file whose name contains a backslash. A
package that means one of those must say which.
"""

from __future__ import annotations

import posixpath
import unicodedata
from collections.abc import Iterable

from .errors import PackageValidationError
from .limits import MAX_PATH_BYTES

_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})


def normalize_package_path(raw: object, *, what: str = "path") -> str:
    """Return the canonical form of a package-relative path, or raise.

    Idempotent: normalizing an already-normalized path returns it unchanged,
    which is what lets the digest and the runtime lookup agree without either
    re-deriving the other's rules.
    """
    if not isinstance(raw, str):
        raise PackageValidationError(f"{what} must be a string, got {type(raw).__name__}")
    if not raw:
        raise PackageValidationError(f"{what} must not be empty")
    if "\x00" in raw:
        raise PackageValidationError(f"{what} contains a NUL byte")
    if "\\" in raw:
        raise PackageValidationError(f"{what} must use '/' separators; backslash is not a separator")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in raw):
        raise PackageValidationError(f"{what} contains a control character")
    if raw.startswith("/"):
        raise PackageValidationError(f"{what} must be relative to the package root")
    if len(raw) > 1 and raw[1] == ":":
        raise PackageValidationError(f"{what} must not be a drive-qualified path")

    normalized = unicodedata.normalize("NFC", raw)
    segments = normalized.split("/")
    for segment in segments:
        if segment in _FORBIDDEN_SEGMENTS:
            raise PackageValidationError(f"{what} has an empty, '.' or '..' segment: {raw!r}")
        if segment.strip() != segment or not segment.strip():
            raise PackageValidationError(f"{what} has a segment with leading/trailing whitespace: {raw!r}")

    # posixpath.normpath is the cross-check, not the normalizer: the segment
    # rules above already reject everything it would collapse, so a value that
    # differs here means a form we have not accounted for.
    if posixpath.normpath(normalized) != normalized:
        raise PackageValidationError(f"{what} is not in normalized form: {raw!r}")

    size = len(normalized.encode("utf-8"))
    if size > MAX_PATH_BYTES:
        raise PackageValidationError(f"{what} is {size} UTF-8 bytes, limit is {MAX_PATH_BYTES}")
    return normalized


def assert_no_case_collisions(paths: Iterable[str]) -> None:
    """Reject a package whose paths differ only by case or Unicode folding.

    Case-sensitivity is part of the package contract, but the *content store*
    lands on whatever filesystem the user has. Two paths that fold together
    would resolve to one file on macOS/Windows and two on Linux, so the digest
    would describe content the host cannot actually reproduce.
    """
    folded: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        previous = folded.get(key)
        if previous is not None and previous != path:
            raise PackageValidationError(f"package paths {previous!r} and {path!r} collide when case is folded")
        folded[key] = path
