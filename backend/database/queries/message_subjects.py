"""The subject tagger's per-reply cache."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from ..connection import immediate_tx, select_rows
from ..models import MessageSubjectsRow


async def get_message_subjects(message_ids: Sequence[int]) -> dict[int, MessageSubjectsRow]:
    if not message_ids:
        return {}
    marks = ",".join("?" * len(message_ids))
    rows = await select_rows(
        f"SELECT message_id, content_hash, version, probs FROM message_subjects WHERE message_id IN ({marks})",  # nosec B608 -- placeholders only
        tuple(message_ids),
    )
    return {
        row["message_id"]: MessageSubjectsRow(
            message_id=row["message_id"],
            content_hash=row["content_hash"],
            version=row["version"],
            probs=json.loads(row["probs"]),
        )
        for row in rows
    }


async def set_message_subjects(message_id: int, content_hash: str, version: str, probs: Mapping[str, Sequence[float]]) -> None:
    """Store (or replace) a reply's tags. Raises ``sqlite3.IntegrityError`` when the message no longer exists."""
    async with immediate_tx() as db:
        await db.execute(
            "INSERT INTO message_subjects (message_id, content_hash, version, probs) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(message_id) DO UPDATE SET content_hash = excluded.content_hash, version = excluded.version, "
            "probs = excluded.probs",
            (message_id, content_hash, version, json.dumps({c: list(p) for c, p in probs.items()})),
        )
