"""Write-only secrets: what a package may reference but never read.

A secret is declared by name in the manifest, filled in by the user through an
Orb-owned form, and referenced from exactly two positions in a flow -- an
``http.request`` header value and its body. Everything else about it is closed:

* **The API is write-only by construction.** There is no query in
  ``database/queries/extensions.py`` that returns a value to a route, and the
  one that does return values (:func:`load_secrets`) is called only by the
  network client, which substitutes and discards.
* **A reference is not a value.** ``{"$secret": "token"}`` survives value
  resolution as a marker; :mod:`.network` turns it into bytes on the way out.
  The interpreter never holds the string, so it cannot reach a template, a log,
  a state write, a UI value, an SSE payload, or a returned error -- not because
  each of those checks for it, but because it was never there.
* **What comes back is scanned.** The literal byte sequence appearing in a
  response is treated as a leak and the response is discarded.

At-rest storage is Orb's ordinary local SQLite posture, and v1 does not claim
otherwise. The improvement this buys is non-disclosure to package logic and to
frontend payloads, which is a real boundary and a smaller one than encryption.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from ...database import (
    get_extension_secret_values,
    write_extension_secrets,
)

MAX_SECRET_BYTES = 4096
"""One stored secret value. An API token, not a payload."""


class SecretError(ValueError):
    """A secret write names something the active manifest does not declare, or
    a value outside the stored bound. Distinct from a package failure: the
    package is fine, the submitted form is not."""


async def load_secrets(extension_id: str, declared: Iterable[str]) -> dict[str, str]:
    """The stored values for the names the *active manifest* declares.

    Filtered by the declaration rather than returned wholesale, so a secret left
    behind by an older revision cannot be substituted into a request the current
    revision's manifest never showed the user. Names with no stored value are
    absent; the substitution step turns that into a clear failure rather than an
    empty header.
    """
    names = {name for name in declared if isinstance(name, str)}
    if not names:
        return {}
    stored = await get_extension_secret_values(extension_id)
    return {name: value for name, value in stored.items() if name in names and value}


async def write_secrets(extension_id: str, declared: Iterable[str], values: Mapping[str, object]) -> list[str]:
    """Set or clear secrets, returning the names that changed.

    ``None`` or an empty string clears one. A name the manifest does not declare
    is refused rather than ignored: the form is generated from the declaration,
    so an undeclared name means the request did not come from that form.
    """
    allowed = {name for name in declared if isinstance(name, str)}
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise SecretError(f"this extension declares no secret named {unknown[0]!r}")

    normalized: dict[str, str | None] = {}
    for name in sorted(values):
        value = values[name]
        if value is None or value == "":
            normalized[name] = None
            continue
        if not isinstance(value, str):
            raise SecretError(f"the value for {name!r} must be a string")
        if len(value.encode("utf-8")) > MAX_SECRET_BYTES:
            raise SecretError(f"the value for {name!r} is over the {MAX_SECRET_BYTES} byte limit")
        normalized[name] = value
    await write_extension_secrets(extension_id, normalized)
    return sorted(normalized)


__all__ = ["MAX_SECRET_BYTES", "SecretError", "load_secrets", "write_secrets"]
