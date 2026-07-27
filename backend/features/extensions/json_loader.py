"""The one JSON parser every package file and every runtime JSON value goes
through.

``json.loads`` on its own accepts several things Orb must reject before a value
becomes a compiled artifact:

* **Duplicate object keys.** ``{"a": 1, "a": 2}`` parses to ``{"a": 2}``. Two
  readers of the same bytes -- Orb's compiler and an author's linter, or Orb
  today and Orb after a parser change -- can legitimately disagree about which
  wins, which means the content digest would no longer pin a single meaning.
* **Non-finite numbers.** ``parse_constant`` catches the ``NaN`` / ``Infinity``
  literals, but ``1e400`` overflows to ``inf`` through ``parse_float`` without
  ever touching it. Both doors are closed here.
* **Unbounded nesting.** The depth pre-scan runs over the raw text *before*
  ``json.loads`` builds anything, so a nesting bomb is rejected without
  allocating the structure it describes.
* **Unbounded breadth.** Member counts and string sizes are checked on an
  explicit-stack walk, never recursively -- the check must not be the thing
  that overflows.

Every rejection is a :class:`PackageParseError` or
:class:`PackageLimitExceeded` naming *what* was read, so an install diagnostic
can say "flows/score.json exceeded the nesting limit" without echoing package
content back to the user.
"""

from __future__ import annotations

import json
import math
from typing import Any

from .errors import PackageLimitExceeded, PackageParseError
from .limits import (
    MAX_JSON_DEPTH,
    MAX_JSON_MEMBERS,
    MAX_JSON_STRING_BYTES,
)

_MAX_INT = 2**63 - 1


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise PackageParseError(f"duplicate object key {key!r}")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise PackageParseError(f"non-finite JSON literal {name!r} is not allowed")


def _parse_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value):
        raise PackageParseError("number literal overflows to a non-finite float")
    return value


def _parse_int(raw: str) -> int:
    value = int(raw)
    if abs(value) > _MAX_INT:
        raise PackageParseError("integer literal exceeds the 64-bit range")
    return value


def _scan_depth(text: str, *, what: str) -> None:
    """Reject nesting past :data:`MAX_JSON_DEPTH` without building the value.

    A single linear pass that tracks string state and backslash escapes, so
    brackets inside string literals never count. Cheaper than parsing and, more
    importantly, it runs first: ``json.loads`` on a 10,000-deep array raises
    ``RecursionError`` (or, on a C-scanner build, allocates the whole thing)
    before any post-parse check could see it.
    """
    depth = 0
    in_string = False
    escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PackageLimitExceeded(f"{what}: JSON nesting exceeds the depth limit of {MAX_JSON_DEPTH}")
        elif ch in "}]":
            depth -= 1


def _walk_limits(value: Any, *, what: str) -> None:
    """Enforce member-count and string-size limits with an explicit stack."""
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if len(node) > MAX_JSON_MEMBERS:
                raise PackageLimitExceeded(f"{what}: object has {len(node)} members, limit is {MAX_JSON_MEMBERS}")
            stack.extend(node.values())
            stack.extend(node.keys())
        elif isinstance(node, list):
            if len(node) > MAX_JSON_MEMBERS:
                raise PackageLimitExceeded(f"{what}: array has {len(node)} members, limit is {MAX_JSON_MEMBERS}")
            stack.extend(node)
        elif isinstance(node, str):
            size = len(node.encode("utf-8"))
            if size > MAX_JSON_STRING_BYTES:
                raise PackageLimitExceeded(f"{what}: string value is {size} bytes, limit is {MAX_JSON_STRING_BYTES}")


def decode_text(data: bytes, *, what: str, max_bytes: int) -> str:
    """Decode package bytes as strict UTF-8 within *max_bytes*.

    Rejects a BOM: the digest is taken over exact bytes for assets and over the
    canonical encoding for JSON, and a leading BOM would make two byte-different
    files carry the same parsed value.
    """
    if len(data) > max_bytes:
        raise PackageLimitExceeded(f"{what}: {len(data)} bytes exceeds the limit of {max_bytes}")
    if data.startswith(b"\xef\xbb\xbf"):
        raise PackageParseError(f"{what}: UTF-8 BOM is not allowed")
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PackageParseError(f"{what}: not valid UTF-8 ({exc.reason})") from None


def load_json(data: bytes | str, *, what: str, max_bytes: int) -> Any:
    """Strictly parse one package JSON document.

    *what* names the document in error messages (``"orb-extension.json"``,
    ``"flows/score-scene.json"``). *max_bytes* is the document's own limit --
    the manifest and an asset do not share one.
    """
    text = decode_text(data, what=what, max_bytes=max_bytes) if isinstance(data, bytes) else data
    if isinstance(data, str) and len(text.encode("utf-8")) > max_bytes:
        raise PackageLimitExceeded(f"{what}: exceeds the limit of {max_bytes} bytes")

    _scan_depth(text, what=what)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
            parse_int=_parse_int,
        )
    except PackageParseError as exc:
        raise PackageParseError(f"{what}: {exc}") from None
    except RecursionError:
        raise PackageLimitExceeded(f"{what}: JSON nesting exceeds the depth limit of {MAX_JSON_DEPTH}") from None
    except ValueError as exc:
        raise PackageParseError(f"{what}: invalid JSON ({exc})") from None

    _walk_limits(value, what=what)
    return value
