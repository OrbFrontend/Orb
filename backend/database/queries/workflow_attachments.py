"""Raw row-level helpers for `workflow_attachments`.

This module is the database boundary for the workflow-attachment cache.
Higher-level concerns (size budget, eviction, access tracking) live in
`backend.workflows.attachment_cache`. The functions here are
side-effect-free with respect to the cache state -- they only read and
write rows.

Provenance guard: `insert_workflow_attachment_row` enforces a non-empty
`workflow_id` so user-upload code paths cannot accidentally land rows in
this table.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import cast

from ...core import scrub_log
from ..connection import get_db
from ..models import WorkflowAttachmentRow

logger = logging.getLogger(__name__)

# Sentinel string written into ``data_b64`` when an artifact's bytes are
# evicted (the other columns stay intact so a later rehydrate can recover the
# bytes from stored parameters). Defined here -- in the database boundary --
# because it describes the persisted shape of the column, not cache policy.
# ``backend.workflows.attachment_cache`` re-exports it for the eviction layer.
EVICTED_MARKER = "[evicted]"


def _staging_root() -> str:
    """Canonical root directory for path-shape attachments.

    Path-shape attachments let a workflow reference a file on disk instead of
    inlining bytes. Since the path can be influenced by user input, each
    ``open()``/``stat()`` call normalizes it with ``realpath`` and rejects it
    unless it lives under this root. Inlined (not a shared helper) so CodeQL
    ``py/path-injection`` can trace the guard to the sink.
    """
    configured = os.environ.get("ORB_WORKFLOW_STAGING_DIR") or tempfile.gettempdir()
    return os.path.realpath(configured)


def _encode_metadata_field(value: object, field_name: str, workflow_id: str, filename: str) -> str | None:
    """JSON-encode a dict-shaped metadata field, or return None for absent/bad shape.

    Non-dict values produce None silently -- the row helper accepts these from
    callers that have already coerced them and from defensive paths upstream.
    A dict containing non-serializable contents (e.g. nested ``set``) trips
    ``TypeError`` from ``json.dumps``; the error is logged and the column is
    written as NULL so the row insert still lands.
    """
    if not isinstance(value, dict):
        return None
    try:
        return json.dumps(value)
    except TypeError:
        logger.warning(
            "workflow %r attachment %r %s contains non-JSON-serializable values; storing NULL",
            scrub_log(workflow_id),
            scrub_log(filename),
            field_name,
        )
        return None


async def get_workflow_attachment_by_id(att_id: int) -> WorkflowAttachmentRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, message_id, mime_type, data_b64, filename, created_at, "
                "workflow_id, parent_attachment_id, annotation, seed, generation_metadata, "
                "consumption_metadata, active_sibling_id, recent_accesses "
                "FROM workflow_attachments WHERE id = ?",
                (att_id,),
            )
        )
        return cast(WorkflowAttachmentRow, dict(rows[0])) if rows else None


async def insert_workflow_attachment_row(
    message_id: int,
    attachment: dict,
    *,
    db=None,
    insert_as_evicted: bool = False,
) -> int:
    """Insert a workflow_attachments row. Returns the new id.

    attachment dict shape:
      filename: str (required)
      mime:     str (required)
      data:     bytes XOR path: str (required; exactly one)
      workflow_id: str non-empty (required)
      parent_attachment_id: int or None (optional; default NULL)
      annotation: str or None (optional; default NULL)
      seed: str or None (optional; default NULL)
      generation_metadata: dict or None (optional; default NULL; JSON-encoded)
      consumption_metadata: dict or None (optional; default NULL; JSON-encoded)

    If ``db`` is provided, the helper runs the SELECT + INSERT on the
    caller's connection without committing -- caller owns the transaction
    lifecycle. When ``db`` is None, the helper opens its own connection
    and commits.

    insert_as_evicted: when True, the row is inserted with the
    EVICTED_MARKER sentinel in ``data_b64`` -- the bytes themselves are
    never stored, and the byte count is therefore unrecoverable from this
    row alone. Only ``seed`` + ``generation_metadata`` make such a row
    recoverable later via ``rehydrate_attachment``; without them the
    artifact is permanently byteless. The empty-data guard still applies:
    a marker still requires a non-empty payload so the would-have-been
    bytes are well-defined.

    Raises:
      ValueError -- empty workflow_id, both-or-neither data/path, wrong
                    types, empty payload.
      OSError    -- path stat or read failure (caller decides whether to
                    surface).
      LookupError -- message_id does not exist.
    """
    workflow_id = attachment.get("workflow_id")
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ValueError(f"workflow_id must be a non-empty string; got {workflow_id!r}")

    has_data = "data" in attachment
    has_path = "path" in attachment
    if has_data == has_path:
        raise ValueError("attachment must have exactly one of 'data' or 'path'")

    # Validate emptiness up front so marker-mode inserts skip byte
    # materialization entirely -- reading multi-GB artifacts only to
    # discard them for EVICTED_MARKER would block unrelated writes while
    # the enclosing BEGIN IMMEDIATE transaction is open. Path-branch
    # emptiness check is stat-driven, matching attachment_cache's
    # _estimate_size and validate_workflow_attachment_shape.
    safe_path: str | None = None
    if has_path:
        path = attachment["path"]
        if not isinstance(path, str):
            raise ValueError(f"path must be a string; got {type(path).__name__}")
        # Confine to the staging root before any stat/open (see _staging_root).
        resolved = os.path.realpath(path)
        if not resolved.startswith(_staging_root() + os.sep):
            raise ValueError("path escapes the workflow staging root")
        safe_path = resolved
        if os.path.getsize(safe_path) == 0:
            raise ValueError("attachment data is empty")
    else:
        raw = attachment["data"]
        if not isinstance(raw, (bytes, bytearray)):
            raise ValueError(f"data must be bytes; got {type(raw).__name__}")
        if not raw:
            raise ValueError("attachment data is empty")

    if insert_as_evicted:
        data_b64 = EVICTED_MARKER
    elif has_path:
        assert safe_path is not None
        with open(safe_path, "rb") as f:
            data_b64 = base64.b64encode(f.read()).decode("ascii")
    else:
        data_b64 = base64.b64encode(bytes(attachment["data"])).decode("ascii")

    filename = attachment.get("filename")
    if not isinstance(filename, str):
        raise ValueError(f"filename must be a string; got {type(filename).__name__}")

    mime = attachment.get("mime")
    if not isinstance(mime, str):
        raise ValueError(f"mime must be a string; got {type(mime).__name__}")

    parent_attachment_id = attachment.get("parent_attachment_id")
    annotation = attachment.get("annotation")
    seed = attachment.get("seed")
    generation_metadata_json = _encode_metadata_field(
        attachment.get("generation_metadata"), "generation_metadata", workflow_id, filename
    )
    consumption_metadata_json = _encode_metadata_field(
        attachment.get("consumption_metadata"), "consumption_metadata", workflow_id, filename
    )

    async def _write(conn) -> int:
        rows = list(await conn.execute_fetchall("SELECT id FROM messages WHERE id = ?", (message_id,)))
        if not rows:
            raise LookupError(f"message_id {message_id!r} does not exist")
        now = datetime.now(timezone.utc).isoformat()
        cur = await conn.execute(
            """INSERT INTO workflow_attachments
               (message_id, mime_type, data_b64, filename, created_at,
                workflow_id, parent_attachment_id, annotation, seed,
                generation_metadata, consumption_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                mime,
                data_b64,
                filename,
                now,
                workflow_id,
                parent_attachment_id,
                annotation,
                seed,
                generation_metadata_json,
                consumption_metadata_json,
            ),
        )
        att_id = cur.lastrowid
        assert att_id is not None
        return att_id

    if db is not None:
        return await _write(db)
    async with get_db() as own_db:
        att_id = await _write(own_db)
        await own_db.commit()
        return att_id
