from __future__ import annotations

import json
from datetime import UTC, datetime
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
        now = datetime.now(UTC).isoformat()
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


# ── Retention
# The Director audit trail is the fastest-growing purely-diagnostic table in the
# schema (one row per turn, each holding full LLM output + reasoning). Nothing
# reads it after the fact except the Inspector, and that route already returns an
# empty shape for a missing log, so pruning old rows degrades gracefully.
#
# ``cutoff`` is an ISO-8601 UTC string, matching how ``created_at`` is written;
# a plain string compare therefore orders correctly. None means "no age limit".

# ponytail: approximate -- sums the fat text columns only, ignoring row and page
# overhead. This is a "is this worth cleaning?" hint for the UI, not accounting.
_LOG_BYTES = (
    "COALESCE(length(agent_raw_output), 0) + COALESCE(length(tool_calls), 0) "
    "+ COALESCE(length(injection_block), 0) + COALESCE(length(reasoning_director), 0) "
    "+ COALESCE(length(reasoning_writer), 0) + COALESCE(length(reasoning_editor), 0)"
)


async def logs_size_before(cutoff: str | None) -> tuple[int, int]:
    """``(count, approx_bytes)`` of the logs ``delete_logs_older_than`` would drop."""
    sql = f"SELECT COUNT(*) AS n, COALESCE(SUM({_LOG_BYTES}), 0) AS b FROM conversation_logs"
    params: tuple[str, ...] = ()
    if cutoff is not None:
        sql += " WHERE created_at < ?"
        params = (cutoff,)
    async with get_db() as db:
        rows = list(await db.execute_fetchall(sql, params))
    return (int(rows[0]["n"]), int(rows[0]["b"])) if rows else (0, 0)


async def delete_logs_older_than(cutoff: str | None) -> int:
    """Drop Director logs created before ``cutoff`` (None = all). Returns rows deleted."""
    sql = "DELETE FROM conversation_logs"
    params: tuple[str, ...] = ()
    if cutoff is not None:
        sql += " WHERE created_at < ?"
        params = (cutoff,)
    async with get_db() as db:
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount or 0
