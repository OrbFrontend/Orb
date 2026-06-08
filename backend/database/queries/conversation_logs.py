from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import cast

from ..connection import get_db
from ..models import ConversationLogRow


async def add_conversation_log(
    cid: str,
    turn_index: int,
    agent_raw: str,
    tool_calls: list,
    styles_after: list,
    injection: str,
    latency_ms: int,
    progressive_fields: dict | None = None,
    message_id: int | None = None,
    reasoning_director: str = "",
    reasoning_writer: str = "",
    reasoning_editor: str = "",
    feedback: dict | None = None,
):
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO conversation_logs (conversation_id, turn_index, agent_raw_output, tool_calls, active_moods_after, progressive_fields_after, injection_block, agent_latency_ms, created_at, message_id, reasoning_director, reasoning_writer, reasoning_editor, feedback) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                turn_index,
                agent_raw,
                json.dumps(tool_calls),
                json.dumps(styles_after),
                json.dumps(progressive_fields or {}),
                injection,
                latency_ms,
                now,
                message_id,
                reasoning_director,
                reasoning_writer,
                reasoning_editor,
                json.dumps(feedback or {}),
            ),
        )
        await db.commit()


async def get_moods_before_turn(cid: str, turn_index: int) -> list[str]:
    """Return active_moods_after from the most recent log entry before turn_index."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT active_moods_after FROM conversation_logs WHERE conversation_id = ? AND turn_index < ? ORDER BY turn_index DESC LIMIT 1",
                (cid, turn_index),
            )
        )
        if rows and rows[0]["active_moods_after"]:
            return json.loads(rows[0]["active_moods_after"])
        return []


async def get_conversation_logs(cid: str) -> list[ConversationLogRow]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM conversation_logs WHERE conversation_id = ? ORDER BY turn_index ASC",
                (cid,),
            )
        )
        result = []
        for r in rows:
            d = dict(r)
            d["tool_calls"] = json.loads(d["tool_calls"]) if d["tool_calls"] else []
            d["active_moods_after"] = json.loads(d["active_moods_after"]) if d["active_moods_after"] else []
            d["feedback"] = json.loads(d["feedback"]) if d.get("feedback") else {}
            result.append(cast(ConversationLogRow, d))
        return result


async def get_director_log_for_message(message_id: int) -> ConversationLogRow | None:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT * FROM conversation_logs WHERE message_id = ? ORDER BY id DESC LIMIT 1",
                (message_id,),
            )
        )
        if not rows:
            return None
        d = dict(rows[0])
        d["tool_calls"] = json.loads(d["tool_calls"]) if d["tool_calls"] else []
        d["active_moods_after"] = json.loads(d["active_moods_after"]) if d["active_moods_after"] else []
        d["feedback"] = json.loads(d["feedback"]) if d.get("feedback") else {}
        d.setdefault("reasoning_director", "")
        d.setdefault("reasoning_writer", "")
        d.setdefault("reasoning_editor", "")
        return cast(ConversationLogRow, d)
