from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast

from ..connection import get_db
from ..models import ConversationLogRow


async def add_conversation_log(
    cid: str,
    turn_index: int,
    tool_calls: list,
    styles_after: list,
    injection: str,
    latency_ms: int,
    message_id: int | None = None,
    reasoning_director: str = "",
    reasoning_writer: str = "",
    reasoning_editor: str = "",
    feedback: dict | None = None,
    fragment_diagnostics: list[dict[str, str]] | None = None,
):
    async with get_db() as db:
        now = datetime.now(UTC).isoformat()
        await db.execute(
            "INSERT INTO conversation_logs (conversation_id, turn_index, tool_calls, active_moods_after, injection_block, agent_latency_ms, created_at, message_id, reasoning_director, reasoning_writer, reasoning_editor, feedback, fragment_diagnostics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                turn_index,
                json.dumps(tool_calls),
                json.dumps(styles_after),
                injection,
                latency_ms,
                now,
                message_id,
                reasoning_director,
                reasoning_writer,
                reasoning_editor,
                json.dumps(feedback or {}),
                json.dumps(fragment_diagnostics or []),
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
            d["fragment_diagnostics"] = json.loads(d["fragment_diagnostics"]) if d.get("fragment_diagnostics") else []
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
        d["fragment_diagnostics"] = json.loads(d["fragment_diagnostics"]) if d.get("fragment_diagnostics") else []
        d.setdefault("reasoning_director", "")
        d.setdefault("reasoning_writer", "")
        d.setdefault("reasoning_editor", "")
        return cast(ConversationLogRow, d)


# ── Retention
# The Director audit trail is the fastest-growing purely-diagnostic table in the
# schema (one row per turn, each holding full LLM output, reasoning and injection
# blocks). Cleanup blanks the payload in place rather than dropping the row: two
# callers read ``active_moods_after`` off old rows to carry mood state forward
# (:func:`get_moods_before_turn` and the branch-switch restore in queries/messages),
# and a missing row silently breaks that continuity.
#
# ``cutoff`` is an ISO-8601 UTC string, matching how ``created_at`` is written;
# a plain string compare therefore orders correctly. None means "no age limit".

# A whitelist, deliberately: every column *not* named here is wiped, so a column
# added to the table later is reclaimed by default instead of quietly growing
# forever. Adding one here is the explicit act, not leaving one out.
LOG_KEEP_COLUMNS = frozenset(
    {
        "id",
        "conversation_id",
        "turn_index",
        "created_at",
        "message_id",
        "active_moods_after",  # mood continuity -- the reason the row survives at all
        "agent_latency_ms",  # /api/stats averages it, and an int has nothing to reclaim
    }
)


async def _wipeable(db) -> list[tuple[str, str, int]]:
    """``(column, blank_sql, blank_len)`` for every non-whitelisted column.

    NOT NULL columns reset to their schema default rather than NULL; readers
    already treat both as "nothing here" (``json.loads(x) if x else {}``).
    ``blank_len`` is what survives the wipe, so the size estimate counts only
    what is actually reclaimable and a second run correctly finds nothing.
    """
    rows = list(await db.execute_fetchall("PRAGMA table_info(conversation_logs)"))
    out = []
    for r in rows:
        if r["name"] in LOG_KEEP_COLUMNS:
            continue
        blank = (r["dflt_value"] or "''") if r["notnull"] else "NULL"
        out.append((r["name"], blank, 0 if blank == "NULL" else len(blank.strip("'"))))
    return out


def _reclaimable(cols: list[tuple[str, str, int]]) -> str:
    return " + ".join(f"MAX(COALESCE(length({c}), 0) - {keep}, 0)" for c, _, keep in cols)


async def logs_size_before(cutoff: str | None) -> tuple[int, int]:
    """``(rows, approx_bytes)`` that :func:`wipe_logs_older_than` would reclaim."""
    async with get_db() as db:
        total = _reclaimable(await _wipeable(db))
        sql = f"SELECT COUNT(*) AS n, COALESCE(SUM({total}), 0) AS b FROM conversation_logs WHERE ({total}) > 0"
        params: tuple[str, ...] = ()
        if cutoff is not None:
            sql += " AND created_at < ?"
            params = (cutoff,)
        rows = list(await db.execute_fetchall(sql, params))
    return (int(rows[0]["n"]), int(rows[0]["b"])) if rows else (0, 0)


async def wipe_logs_older_than(cutoff: str | None) -> int:
    """Blank the payload of Agent logs older than ``cutoff`` (None = all).

    Returns rows touched. Already-blank rows are excluded so the count matches
    what the preview promised and a repeat run is a no-op.
    """
    async with get_db() as db:
        cols = await _wipeable(db)
        sets = ", ".join(f"{c} = {blank}" for c, blank, _ in cols)
        sql = f"UPDATE conversation_logs SET {sets} WHERE ({_reclaimable(cols)}) > 0"
        params: tuple[str, ...] = ()
        if cutoff is not None:
            sql += " AND created_at < ?"
            params = (cutoff,)
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount or 0
