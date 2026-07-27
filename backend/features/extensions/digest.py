"""Canonical JSON encoding and the package content digest.

One digest algorithm is shared by Git inspection, archive inspection, install
revalidation, startup reconciliation, update, and the author CLI. If any two of
those computed it differently, a package that inspected clean would activate as
something else -- so the algorithm lives here and nowhere else.

    digest = SHA-256(
        b"orb-extension-content-v1\\n"
      || u64_be(file_count)
      || for each selected path, ordered by its UTF-8 bytes:
             u64_be(len(path_bytes)) || path_bytes
          || u64_be(len(content))    || content
    )

Three properties are load-bearing:

* **Domain separation.** The version prefix means a future algorithm change
  cannot produce a colliding digest for the same content under v1 rules.
* **Length delimiting.** Without the ``u64`` prefixes, the pair
  ``{"a/b": "c"}`` and ``{"a": "b/c"}`` would concatenate to the same bytes.
* **Canonical JSON content.** A JSON file contributes the canonical encoding of
  its *strictly parsed value*, not its source bytes. Reformatting a flow --
  whitespace, key order, ``\\u`` escaping of an ASCII character -- must not
  change the identity of a package that behaves identically. Non-JSON assets
  contribute exact bytes, because for an image "behaves identically" is not a
  property Orb can evaluate.

Only manifest-*referenced* files are selected. An unreferenced README edit does
not produce a new revision, because Orb never compiled, persisted, or served
that file in the first place.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .paths import assert_no_case_collisions, normalize_package_path

DIGEST_DOMAIN = b"orb-extension-content-v1\n"

ContentKind = Literal["json", "binary"]


@dataclass(frozen=True, slots=True)
class PackageContent:
    """One selected package file as the digest sees it.

    ``kind="json"`` carries the already strictly-parsed value (see
    :mod:`.json_loader`); ``kind="binary"`` carries exact bytes. The two are
    deliberately different types so a caller cannot pass raw JSON bytes and
    silently get source-byte hashing for a file whose formatting should not
    matter.
    """

    kind: ContentKind
    value: Any = None
    data: bytes = b""

    @staticmethod
    def json(value: Any) -> PackageContent:
        return PackageContent(kind="json", value=value)

    @staticmethod
    def binary(data: bytes) -> PackageContent:
        return PackageContent(kind="binary", data=bytes(data))

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.value) if self.kind == "json" else self.data


def canonical_json_bytes(value: Any) -> bytes:
    """Orb's canonical encoding of a JSON value.

    Sorted keys, no insignificant whitespace, no ``NaN``/``Infinity``, and
    non-ASCII characters emitted literally as UTF-8 rather than ``\\u`` escapes
    -- one encoding per value, so the digest is a function of meaning.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _u64(n: int) -> bytes:
    return n.to_bytes(8, "big")


def content_digest(files: Mapping[str, PackageContent]) -> str:
    """Return the lowercase hex SHA-256 content digest of a selected file set.

    Paths are normalized and checked for case-folding collisions first, so two
    packages that this host cannot store distinctly can never share a digest.
    """
    normalized: dict[str, PackageContent] = {}
    for raw_path, content in files.items():
        path = normalize_package_path(raw_path, what="package file path")
        if path in normalized:
            raise ValueError(f"duplicate normalized package path {path!r}")
        normalized[path] = content
    assert_no_case_collisions(normalized)

    h = hashlib.sha256()
    h.update(DIGEST_DOMAIN)
    h.update(_u64(len(normalized)))
    for path in sorted(normalized, key=lambda p: p.encode("utf-8")):
        path_bytes = path.encode("utf-8")
        body = normalized[path].canonical_bytes()
        h.update(_u64(len(path_bytes)))
        h.update(path_bytes)
        h.update(_u64(len(body)))
        h.update(body)
    return h.hexdigest()
