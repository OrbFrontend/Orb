"""Remove saved image reviews and obsolete refinement timeline metadata."""

from __future__ import annotations

import json
import sqlite3

_REVIEW_NOTES = (
    "the prompter did not review this render:",
    "the prompter gave no usable review of this render",
    "the prompter asked for another render but wrote no revised prompt",
    "the review call failed:",
    "the revised render failed:",
)


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(workflow_attachments)")}
    if not {"id", "workflow_id", "consumption_metadata"} <= columns:
        return
    rows = conn.execute("SELECT id, consumption_metadata FROM workflow_attachments WHERE workflow_id = 'image_gen'").fetchall()
    for attachment_id, raw in rows:
        try:
            metadata = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        before = dict(metadata)
        metadata.pop("review", None)
        metadata.pop("refine", None)
        notes = metadata.get("notes")
        if isinstance(notes, list):
            remaining = [note for note in notes if not (isinstance(note, str) and note.startswith(_REVIEW_NOTES))]
            if remaining:
                metadata["notes"] = remaining
            else:
                metadata.pop("notes", None)
        if metadata != before:
            conn.execute(
                "UPDATE workflow_attachments SET consumption_metadata = ? WHERE id = ?", (json.dumps(metadata), attachment_id)
            )
