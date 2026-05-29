from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from ..connection import get_db
from .conversations import get_conversation


async def get_path_to_leaf(cid: str, leaf_id: int) -> list[dict]:
    """Walk parent_id chain from leaf to root, return ordered root→leaf."""
    async with get_db() as db:
        path = []
        current_id = leaf_id
        while current_id is not None:
            rows = list(
                await db.execute_fetchall(
                    "SELECT * FROM messages WHERE id = ? AND conversation_id = ?",
                    (current_id, cid),
                )
            )
            if not rows:
                break
            msg = dict(rows[0])
            raw_pf = msg.get("progressive_fields")
            msg["progressive_fields"] = json.loads(raw_pf) if raw_pf else {}
            path.append(msg)
            current_id = msg.get("parent_id")
        path.reverse()
        return path


async def _attach_user_attachments(messages: list[dict]) -> None:
    if not messages:
        return
    ids = [m["id"] for m in messages]
    placeholders = ",".join("?" * len(ids))
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT id, message_id, mime_type, data_b64, filename, size, created_at "
                f"FROM user_attachments WHERE message_id IN ({placeholders}) ORDER BY id",  # nosec B608
                ids,
            )
        )
    by_msg: dict[int, list] = {m["id"]: [] for m in messages}
    for r in rows:
        by_msg[r["message_id"]].append(dict(r))
    for m in messages:
        m["user_attachments"] = by_msg[m["id"]]


async def _attach_workflow_attachments(messages: list[dict]) -> None:
    if not messages:
        return
    ids = [m["id"] for m in messages]
    placeholders = ",".join("?" * len(ids))
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT id, message_id, mime_type, data_b64, filename, created_at, "
                f"workflow_id, parent_attachment_id, annotation, seed, generation_metadata, "
                f"consumption_metadata, active_sibling_id, recent_accesses "
                f"FROM workflow_attachments WHERE message_id IN ({placeholders}) ORDER BY id",  # nosec B608
                ids,
            )
        )
    by_msg: dict[int, list] = {m["id"]: [] for m in messages}
    for r in rows:
        by_msg[r["message_id"]].append(dict(r))
    for m in messages:
        m["workflow_attachments"] = by_msg[m["id"]]


async def _attach_attachments(messages: list[dict]) -> None:
    await _attach_user_attachments(messages)
    await _attach_workflow_attachments(messages)


async def get_messages(cid: str) -> list[dict]:
    """Get active path messages (root→leaf) for LLM prompt construction."""
    conv = await get_conversation(cid)
    if not conv:
        return []
    leaf_id = conv.get("active_leaf_id")
    if not leaf_id:
        return []
    messages = await get_path_to_leaf(cid, leaf_id)
    await _attach_attachments(messages)
    return messages


async def get_messages_before(cid: str, message_id: int) -> list[dict]:
    """Return active-path messages strictly before ``message_id``.

    The result is shaped to pass through ``format_message_with_attachments``
    unchanged: both ``user_attachments`` and ``workflow_attachments`` are
    populated, and the order is root-to-leaf so prefix builders can splice
    it onto history without reordering.

    Returns [] for missing, foreign-conversation, or root anchors.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT parent_id FROM messages WHERE id = ? AND conversation_id = ?",
                (message_id, cid),
            )
        )
    if not rows:
        return []
    parent_id = rows[0]["parent_id"]
    if parent_id is None:
        return []
    messages = await get_path_to_leaf(cid, parent_id)
    await _attach_attachments(messages)
    return messages


async def get_messages_with_branch_info(cid: str) -> list[dict]:
    """Get active path messages with branch navigation metadata for the frontend."""
    messages = await get_messages(cid)
    if not messages:
        return []
    async with get_db() as db:
        for msg in messages:
            parent_id = msg.get("parent_id")
            if parent_id is None:
                sibling_rows = list(
                    await db.execute_fetchall(
                        "SELECT id FROM messages WHERE conversation_id = ? AND parent_id IS NULL ORDER BY id ASC",
                        (cid,),
                    )
                )
            else:
                sibling_rows = list(
                    await db.execute_fetchall(
                        "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? ORDER BY id ASC",
                        (cid, parent_id),
                    )
                )
            sibling_ids = [r["id"] for r in sibling_rows]
            idx = sibling_ids.index(msg["id"]) if msg["id"] in sibling_ids else 0
            msg["branch_count"] = len(sibling_ids)
            msg["branch_index"] = idx
            msg["prev_branch_id"] = sibling_ids[idx - 1] if idx > 0 else None
            msg["next_branch_id"] = sibling_ids[idx + 1] if idx < len(sibling_ids) - 1 else None
    return messages


async def add_message(
    cid: str,
    role: str,
    content: str,
    turn_index: int,
    parent_id: int | None = None,
    attachments: Optional[List[dict]] = None,
    progressive_fields: dict | None = None,
) -> tuple[int, list[dict]]:
    """Add a message. Returns ``(message_id, rejected_workflow_atts)``.

    The rejected list is populated when the workflow batch dropped atts
    for rehydratability reasons (oversize without seed+generation_metadata);
    the message and user atts still commit in that case. Callers are
    expected to surface the rejection -- the orchestrator emits a
    ``workflow_attachments_rejected`` SSE event on the assistant-persist
    path.

    Each attachment dict is one of two shapes, distinguished by an
    in-memory 'source' routing key on the dict (not a persisted column
    -- table identity carries provenance):

    - User uploads (no 'source' or 'source' == 'user'): expects
      'mime_type' (str), 'data_b64' (str), and optional 'filename' /
      'size'. Lands in `user_attachments` via a direct INSERT inside
      this function's transaction.
    - Workflow artifacts ('source' starts with 'workflow:'): expects
      'mime' (str), 'data' (bytes), 'filename' (str), 'workflow_id'
      (str), plus optional 'parent_attachment_id', 'annotation',
      'seed', and 'generation_metadata'. Lands in `workflow_attachments`
      through the cache module's batch entry point.
    """
    workflow_atts: list[dict] = []
    user_atts: list[dict] = []
    for att in attachments or []:
        src = att.get("source")
        if isinstance(src, str) and src.startswith("workflow:"):
            workflow_atts.append(att)
        else:
            user_atts.append(att)

    rejected_workflow_atts: list[dict] = []

    async with get_db() as db:
        # BEGIN IMMEDIATE so the workflow batch's read-then-evict-then-
        # insert sequence executes under the write lock alongside the
        # message INSERT. The cache helper enforces this -- it raises
        # if its conn is not already in a transaction.
        await db.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat()
        try:
            cur = await db.execute(
                "INSERT INTO messages (conversation_id, role, content, turn_index, parent_id, progressive_fields, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    role,
                    content,
                    turn_index,
                    parent_id,
                    json.dumps(progressive_fields or {}),
                    now,
                ),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(f"Foreign key constraint failed for conversation={cid}, parent={parent_id}: {e}") from e
        message_id = cur.lastrowid
        assert message_id is not None
        for att in user_atts:
            await db.execute(
                "INSERT INTO user_attachments (message_id, mime_type, data_b64, filename, size, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    att["mime_type"],
                    att["data_b64"],
                    att.get("filename"),
                    att.get("size"),
                    now,
                ),
            )
        # Lazy import: the database package must not depend on
        # secondary_workflows at import time (would invert the layering).
        if workflow_atts:
            from backend.secondary_workflows.attachment_cache import insert_workflow_attachments

            _, rejected_workflow_atts = await insert_workflow_attachments(message_id, workflow_atts, db=db)
        await db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, cid))
        await db.commit()

    return message_id, rejected_workflow_atts


async def get_user_attachments_for_message(message_id: int) -> List[dict]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, mime_type, data_b64, filename, size, created_at "
                "FROM user_attachments WHERE message_id = ? ORDER BY id",
                (message_id,),
            )
        )
        return [dict(r) for r in rows]


async def get_workflow_attachments_for_message(message_id: int) -> List[dict]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, mime_type, data_b64, filename, created_at, "
                "workflow_id, parent_attachment_id, annotation, seed, generation_metadata, "
                "consumption_metadata, active_sibling_id, recent_accesses "
                "FROM workflow_attachments WHERE message_id = ? ORDER BY id",
                (message_id,),
            )
        )
        return [dict(r) for r in rows]


async def update_message_content(msg_id: int, content: str) -> None:
    """Update the content of an existing message."""
    async with get_db() as db:
        await db.execute("UPDATE messages SET content = ? WHERE id = ?", (content, msg_id))
        await db.commit()


async def get_message_by_id(msg_id: int) -> dict | None:
    """Fetch a single message by its primary key."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,)))
        return dict(rows[0]) if rows else None


async def set_active_leaf(cid: str, leaf_id: int | None):
    """Update the active_leaf_id for a conversation."""
    async with get_db() as db:
        if leaf_id is not None:
            rows = list(
                await db.execute_fetchall(
                    "SELECT id FROM messages WHERE id = ? AND conversation_id = ?",
                    (leaf_id, cid),
                )
            )
            if not rows:
                raise ValueError(f"Message {leaf_id} does not exist in conversation {cid}")
        await db.execute("UPDATE conversations SET active_leaf_id = ? WHERE id = ?", (leaf_id, cid))
        await db.commit()


async def get_deepest_descendant(cid: str, message_id: int) -> int:
    """Return the deepest descendant of message_id (most recently added child chain)."""
    async with get_db() as db:
        current_id = message_id
        while True:
            rows = list(
                await db.execute_fetchall(
                    "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? ORDER BY id DESC LIMIT 1",
                    (cid, current_id),
                )
            )
            if not rows:
                break
            current_id = rows[0]["id"]
        return current_id


async def switch_to_branch(cid: str, message_id: int) -> bool:
    """Set active leaf to the deepest descendant of message_id. Returns False if not found."""
    msg = await get_message_by_id(message_id)
    if not msg or msg["conversation_id"] != cid:
        return False
    leaf_id = await get_deepest_descendant(cid, message_id)
    await set_active_leaf(cid, leaf_id)
    return True


async def get_workflow_message_state(message_id: int, workflow_id: str) -> dict | None:
    """Return the workflow's slot on this message, or None if message missing or slot empty."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT json_extract(workflow_state, '$.' || ?) AS slot FROM messages WHERE id = ?",
                (workflow_id, message_id),
            )
        )
        if not rows:
            return None
        slot = rows[0]["slot"]
        if slot is None:
            return None
        return json.loads(slot)


async def set_workflow_message_state(message_id: int, workflow_id: str, payload: dict | None) -> None:
    """Atomic per-slot write via SQLite JSON1.

    payload=None removes the slot. Empty dict stores {}. No-op if message
    missing (UPDATE matches zero rows).

    Read-modify-write callers must hold
    ``backend.locks.workflow_state_lock(conversation_id, workflow_id)`` (the
    message's owning conversation) across the read-then-write the payload was
    computed from, or a concurrent caller can clobber the read between read
    and write. Acquisition sites: ``backend.main.api_trigger_workflow`` and
    the pre/post pipeline hook loops in ``backend.orchestrator``. The blind
    first write from ``_persist_result`` to a just-minted assistant message
    is exempt: that row is not yet the active leaf and no other caller can
    name its id, so there is nothing to serialize against.
    """
    async with get_db() as db:
        if payload is None:
            await db.execute(
                "UPDATE messages "
                "SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) "
                "WHERE id = ?",
                (workflow_id, message_id),
            )
        else:
            await db.execute(
                "UPDATE messages "
                "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                "WHERE id = ?",
                (workflow_id, json.dumps(payload), message_id),
            )
        await db.commit()


async def delete_message_with_descendants(cid: str, msg_id: int) -> bool:
    """Delete a message, all its siblings, and all their descendants. Updates active_leaf_id if the active branch is affected."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT parent_id FROM messages WHERE id = ? AND conversation_id = ?",
                (msg_id, cid),
            )
        )
        if not rows:
            return False
        parent_id = rows[0]["parent_id"]

        # Collect all siblings (messages with the same parent_id) and their descendants via recursive CTE
        # For root messages (parent_id IS NULL), match other root messages
        if parent_id is not None:
            sibling_cond = "parent_id = ?"
            sibling_params = (parent_id,)
        else:
            sibling_cond = "parent_id IS NULL"
            sibling_params = ()

        desc_rows = list(
            await db.execute_fetchall(
                f"""
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM messages WHERE conversation_id = ? AND {sibling_cond}
                UNION ALL
                SELECT m.id FROM messages m
                INNER JOIN subtree s ON m.parent_id = s.id
                WHERE m.conversation_id = ?
            )
            SELECT id FROM subtree
        """,
                (cid, *sibling_params, cid),
            )
        )
        deleted_ids = {r["id"] for r in desc_rows}

        if not deleted_ids:
            return False

        # If the active leaf is inside the deleted subtree, find a new active leaf
        # Since all siblings are deleted, the new active leaf will be the parent (or NULL for root)
        conv_rows = list(await db.execute_fetchall("SELECT active_leaf_id FROM conversations WHERE id = ?", (cid,)))
        if conv_rows and conv_rows[0]["active_leaf_id"] in deleted_ids:
            new_leaf = parent_id  # parent_id is None for root messages, which is valid

            await db.execute(
                "UPDATE conversations SET active_leaf_id = ? WHERE id = ?",
                (new_leaf, cid),
            )

        placeholders = ",".join("?" * len(deleted_ids))
        await db.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})",  # nosec B608 — placeholders is only '?' chars, ids are parameterised
            list(deleted_ids),
        )

        # Restore director_state to match the new active leaf's turn
        conv_after = list(await db.execute_fetchall("SELECT active_leaf_id FROM conversations WHERE id = ?", (cid,)))
        new_leaf_id = conv_after[0]["active_leaf_id"] if conv_after else None
        if new_leaf_id is not None:
            leaf_row = list(await db.execute_fetchall("SELECT turn_index FROM messages WHERE id = ?", (new_leaf_id,)))
            if leaf_row:
                turn_idx = leaf_row[0]["turn_index"]
                log_row = list(
                    await db.execute_fetchall(
                        "SELECT active_moods_after FROM conversation_logs WHERE conversation_id = ? AND turn_index = ? ORDER BY id DESC LIMIT 1",
                        (cid, turn_idx),
                    )
                )
                restored = json.loads(log_row[0]["active_moods_after"]) if log_row and log_row[0]["active_moods_after"] else []
                await db.execute(
                    "UPDATE director_state SET active_moods = ? WHERE conversation_id = ?",
                    (json.dumps(restored), cid),
                )
        else:
            # No messages left; reset styles
            await db.execute(
                "UPDATE director_state SET active_moods = '[]' WHERE conversation_id = ?",
                (cid,),
            )

        await db.commit()
        return True
