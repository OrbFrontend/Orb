"""Emitted artifacts: staged bytes, recovery metadata, and revision binding.

Community packages reuse the existing workflow attachment cache -- its byte
budget, LRU-3 eviction, sibling grouping, and regenerate/reroll routes -- and
receive none of its filesystem surface. A flow produces bytes it already holds;
this module turns them into the attachment dict the framework persists, and
turns a stored attachment back into the input a recovery flow runs on.

Two facts make recovery honest across an update:

* **The metadata records which revision produced the bytes.** Extension id,
  version, and content digest ride in ``generation_metadata`` beside the
  package's own recovery payload. Regenerate and reroll execute the revision in
  the request's captured registry snapshot under *live* grants -- Orb never
  resurrects the producing revision or an old permission set to satisfy a
  button, and a concurrent update cannot change an invocation midway through.
* **An incompatible recovery input fails loudly and changes nothing.** If the
  active revision's ``recovery_input_schema`` rejects the stored payload, the
  operation returns a sanitized "produced by an incompatible revision"
  diagnostic and leaves the existing attachment exactly as it was. Silently
  regenerating from a different contract would produce bytes the user has no
  reason to trust.

Nothing here writes to the database. The adapter commits a post-hook artifact
with the assistant message and an action's artifact through the ordinary
attachment insert; this module only decides what those calls are handed.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .errors import FlowError
from .limits import MAX_ARTIFACT_FILENAME_CHARS

RECOVERY_KEY = "orb_extension"
"""Where the host's half of the recovery metadata lives.

Namespaced under one key so the package's own ``recovery`` payload keeps the
whole rest of the object and cannot collide with -- or overwrite -- the fields
that identify the producing revision."""


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    """One artifact a flow wants attached, held until the flow returns.

    ``message_id`` is set only in an action, where the target must be named and
    proved to belong to the invocation's conversation. A post hook leaves it
    ``None``: its target is the assistant row being persisted, which has no id
    yet, so the effect rides the pipeline result and commits with that row.
    """

    filename: str
    mime: str
    data: bytes
    annotation: str | None = None
    recovery: Mapping[str, Any] | None = None
    message_id: int | None = None


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def safe_filename(raw: Any, *, fallback: str) -> str:
    """Reduce a package-supplied filename to an inert basename.

    The name is stored, echoed to the frontend, and offered as a download, so it
    goes through the same treatment a package path does and then some: the
    directory component is dropped outright, anything outside a conservative
    character set becomes ``_``, and a leading dot cannot survive. A package that
    wanted ``../../.bashrc`` gets ``bashrc``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    base = posixpath.basename(raw.replace("\\", "/")).strip()
    cleaned = _UNSAFE_FILENAME.sub("_", base).lstrip(".")[:MAX_ARTIFACT_FILENAME_CHARS]
    return cleaned or fallback


def generation_metadata(
    *,
    extension_id: str,
    version: str,
    content_digest: str,
    recovery: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The recovery record stored beside an emitted artifact.

    The package's payload is spread at the top level so a recovery flow reads it
    as ordinary ``input.*``, and the host's provenance sits under one namespaced
    key. Regenerate/reroll validate the payload against the *active* revision's
    declared schema before running anything.
    """
    payload: dict[str, Any] = dict(recovery or {})
    payload[RECOVERY_KEY] = {
        "extension_id": extension_id,
        "version": version,
        "content_digest": content_digest,
    }
    return payload


def attachment_payload(
    staged: StagedArtifact,
    *,
    extension_id: str,
    version: str,
    content_digest: str,
    seed: str,
) -> dict[str, Any]:
    """The attachment dict the framework's insert path expects.

    ``source`` and ``workflow_id`` are stamped here rather than by the package:
    the pipeline bridge validates that pairing before it stages anything, and a
    package that could choose either would be choosing which workflow owns the
    artifact it produced.
    """
    return {
        "workflow_id": extension_id,
        "source": f"workflow:{extension_id}",
        "filename": staged.filename,
        "mime": staged.mime,
        "data": staged.data,
        "seed": seed,
        "generation_metadata": generation_metadata(
            extension_id=extension_id,
            version=version,
            content_digest=content_digest,
            recovery=staged.recovery,
        ),
        "annotation": staged.annotation,
    }


def recovery_input(stored: Mapping[str, Any] | None, *, schema: Any) -> dict[str, Any]:
    """The validated recovery payload a regenerate/reroll flow runs on.

    Raises :class:`~.errors.FlowError` with the design's exact diagnostic when
    the active revision cannot accept what an older one recorded. That is the
    whole point of the check: the alternative is executing the current flow on
    a shape it was not written for, which produces plausible bytes from an
    unrelated contract.
    """
    payload = {key: value for key, value in (stored or {}).items() if key != RECOVERY_KEY}
    if schema is not None:
        reason = schema.validate(payload)
        if reason is not None:
            raise FlowError("this artifact was produced by an incompatible revision of the extension")
    return payload


def producing_revision(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    """The provenance half of a stored artifact's metadata, or ``{}``."""
    record = (stored or {}).get(RECOVERY_KEY)
    return dict(record) if isinstance(record, Mapping) else {}


__all__ = [
    "RECOVERY_KEY",
    "StagedArtifact",
    "attachment_payload",
    "generation_metadata",
    "producing_revision",
    "recovery_input",
    "safe_filename",
]
