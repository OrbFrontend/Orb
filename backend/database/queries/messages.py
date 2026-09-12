from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from ...core.domain_types import MessageRole
from ..connection import _get_workflow_slot, _set_workflow_slot, get_db
from ..models import (
    MessageRow,
    MessageWithAttachments,
    UserAttachmentRow,
    WorkflowAttachmentRowBase,
)
from .conversations import get_conversation


class _WorkflowAttachmentPersister(Protocol):
    """Persists workflow attachments inside ``add_message``'s transaction.

    Implemented by ``backend.workflows.attachment_cache.insert_workflow_attachments``
    and registered at import time -- see the dependency-inversion note below.
    """

    async def __call__(self, message_id: int, attachments: list[dict], *, db: Any = None) -> tuple[list[int], list[dict]]: ...


# Dependency-inversion seam. ``add_message`` must insert workflow attachments
# inside its own write transaction (the cache layer's read->evict->insert runs
# under the same lock as the message INSERT), yet the database layer must never
# import "up" into ``backend.workflows``. So the workflow layer registers its
# persister here at import time and ``add_message`` calls through this slot.
# Left None in DB-only contexts that never produce workflow attachments; in
# that state a workflow attachment reaching ``add_message`` is a wiring bug, so
# we fail loudly rather than silently dropping bytes.
_workflow_attachment_persister: _WorkflowAttachmentPersister | None = None


def register_workflow_attachment_persister(fn: _WorkflowAttachmentPersister) -> None:
    """Wire the workflow-attachment persister into ``add_message``.

    Called once, at import of ``backend.workflows.attachment_cache``.
    """
    global _workflow_attachment_persister
    _workflow_attachment_persister = fn


#: The recursive step's conversation guard, with SQLite's ``+`` no-index
#: operator on it.
#:
#: Every message subtree walk constrains the recursive step by ``parent_id``
#: *and* ``conversation_id``. Left alone, the planner has no statistics for the
#: recursive table and picks the ``conversation_id`` index, which turns each
#: step into "scan every message in the conversation, then scan the rows found
#: so far" -- quadratic in the conversation. ``+`` makes that term unindexable,
#: so the only usable constraint is ``parent_id`` and the step becomes a seek.
#: The guard still runs as a filter, so the query means exactly what it did
#: before. Measured on a 3,000-message chat: 716 ms -> 2.2 ms.
_SAME_CONVERSATION = "+m.conversation_id = ?"

#: SQLite's default ``SQLITE_MAX_VARIABLE_NUMBER`` is 999 on older builds.
_SQL_PARAM_CHUNK = 900


def _chunked(items: list[int], size: int) -> list[list[int]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _levels_deepest_first(pairs: Sequence[tuple[int, int]]) -> list[list[int]]:
    """Group ``(depth, id)`` pairs into id lists, deepest depth first."""
    by_depth: dict[int, list[int]] = {}
    for depth, message_id in pairs:
        by_depth.setdefault(depth, []).append(message_id)
    return [by_depth[d] for d in sorted(by_depth, reverse=True)]


async def get_path_to_leaf(cid: str, leaf_id: int) -> list[MessageWithAttachments]:
    """Walk parent_id chain from leaf to root, return ordered root→leaf."""
    async with get_db() as db:
        # One recursive walk rather than one round trip per message: a long
        # chat's active path is thousands of rows deep and every read of it
        # paid for that chain. The recursive step stops on a parent that is
        # missing or in another conversation, which is where the old loop
        # broke. ``depth`` counts up from the leaf, so ordering by it
        # descending yields root->leaf without a reverse.
        rows = list(
            await db.execute_fetchall(
                f"""
                WITH RECURSIVE ancestors(depth, id, parent_id) AS (
                    SELECT 0, id, parent_id FROM messages WHERE id = ? AND conversation_id = ?
                    UNION ALL
                    SELECT a.depth + 1, m.id, m.parent_id FROM messages m
                    JOIN ancestors a ON m.id = a.parent_id
                    WHERE {_SAME_CONVERSATION}
                )
                SELECT m.* FROM ancestors a
                JOIN messages m ON m.id = a.id
                ORDER BY a.depth DESC
                """,
                (leaf_id, cid, cid),
            )
        )
        path: list[MessageWithAttachments] = []
        for row in rows:
            # Raw row: progressive_fields is still the JSON string here. Decode
            # it before labeling the dict a MessageWithAttachments, whose
            # progressive_fields is typed as the decoded dict.
            msg = dict(row)
            raw_pf = msg.get("progressive_fields")
            msg["progressive_fields"] = json.loads(raw_pf) if raw_pf else {}
            path.append(cast(MessageWithAttachments, msg))
        return path


_USER_ATTACHMENT_COLUMNS = "id, message_id, mime_type, data_b64, filename, size, created_at"
_WORKFLOW_ATTACHMENT_COLUMNS = (
    "id, message_id, mime_type, data_b64, filename, created_at, "
    "workflow_id, parent_attachment_id, annotation, seed, generation_metadata, "
    "consumption_metadata, active_sibling_id, recent_accesses"
)


async def _child_rows_by_message(messages: list[MessageWithAttachments], *, table: str, columns: str) -> dict[int, list]:
    """Fetch per-message child rows from *table* grouped by ``message_id``.

    Every message id gets an entry (empty list when it has no children), so
    callers can assign unconditionally.
    """
    ids = [m["id"] for m in messages]
    placeholders = ",".join("?" * len(ids))
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT {columns} FROM {table} WHERE message_id IN ({placeholders}) ORDER BY id",  # nosec B608 -- table/columns are module literals; values parameterised
                ids,
            )
        )
    by_msg: dict[int, list] = {m["id"]: [] for m in messages}
    for r in rows:
        by_msg[r["message_id"]].append(dict(r))
    return by_msg


async def _attach_attachments(messages: list[MessageWithAttachments]) -> None:
    if not messages:
        return
    user_by_msg = await _child_rows_by_message(messages, table="user_attachments", columns=_USER_ATTACHMENT_COLUMNS)
    wf_by_msg = await _child_rows_by_message(messages, table="workflow_attachments", columns=_WORKFLOW_ATTACHMENT_COLUMNS)
    for m in messages:
        m["user_attachments"] = user_by_msg[m["id"]]
        m["workflow_attachments"] = wf_by_msg[m["id"]]


async def get_messages(cid: str) -> list[MessageWithAttachments]:
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


async def get_messages_before(cid: str, message_id: int) -> list[MessageWithAttachments]:
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


async def get_messages_with_branch_info(cid: str) -> list[MessageWithAttachments]:
    """Get active path messages with branch navigation metadata for the frontend."""
    messages = await get_messages(cid)
    if not messages:
        return []
    # One query for every sibling set on the path, not one per message: the
    # parents are exactly the path's own ids shifted by one, so a single
    # ``parent_id IN (...)`` covers them all and the grouping happens here.
    parent_ids = sorted({int(p) for m in messages if (p := m.get("parent_id")) is not None})
    siblings_by_parent: dict[int | None, list[int]] = {}
    async with get_db() as db:
        # Root messages are their own sibling set, and no ``IN`` list can hold
        # NULL, so they need the one extra query.
        if any(m.get("parent_id") is None for m in messages):
            siblings_by_parent[None] = [
                r["id"]
                for r in await db.execute_fetchall(
                    "SELECT id FROM messages WHERE conversation_id = ? AND parent_id IS NULL ORDER BY id ASC",
                    (cid,),
                )
            ]
        for chunk in _chunked(parent_ids, _SQL_PARAM_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = await db.execute_fetchall(
                # ORDER BY parent_id, id keeps each group contiguous and each
                # group's ids ascending, which is the order the branch pager
                # numbers swipes in.
                f"SELECT id, parent_id FROM messages WHERE conversation_id = ? AND parent_id IN ({placeholders}) "  # nosec B608 — placeholders is only '?' chars, ids are parameterised
                "ORDER BY parent_id ASC, id ASC",
                (cid, *chunk),
            )
            for r in rows:
                siblings_by_parent.setdefault(r["parent_id"], []).append(r["id"])

    for msg in messages:
        sibling_ids = siblings_by_parent.get(msg.get("parent_id"), [])
        idx = sibling_ids.index(msg["id"]) if msg["id"] in sibling_ids else 0
        msg["branch_count"] = len(sibling_ids)
        msg["branch_index"] = idx
        msg["prev_branch_id"] = sibling_ids[idx - 1] if idx > 0 else None
        msg["next_branch_id"] = sibling_ids[idx + 1] if idx < len(sibling_ids) - 1 else None
    return messages


def user_attachment_payloads(msg: Mapping[str, Any]) -> list[dict] | None:
    """Map a loaded message's ``user_attachments`` into ``add_message`` payloads.

    Conversation-fork flows (Compress History, Checkpoint) re-append messages
    onto a copy and must carry the user uploads while dropping regenerable
    workflow attachments. Returns ``None`` when there are no uploads, matching
    ``add_message``'s ``attachments`` default so the result passes straight
    through.
    """
    atts = msg.get("user_attachments") or []
    if not atts:
        return None
    return [
        {
            "mime_type": a.get("mime_type"),
            "data_b64": a.get("data_b64"),
            "filename": a.get("filename"),
            "size": a.get("size"),
        }
        for a in atts
    ]


async def add_message(
    cid: str,
    role: MessageRole,
    content: str,
    turn_index: int,
    parent_id: int | None = None,
    attachments: Sequence[Mapping[str, Any]] | None = None,
    progressive_fields: dict | None = None,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
    writer_draft: str | None = None,
    advance_leaf: bool = False,
) -> tuple[int, list[dict]]:
    """Insert a message and return its id and rejected attachments."""
    # workflow atts are materialized into a fresh list[dict] the cache writer
    # owns and mutates (it tags rejects with a 'reason' and shallow-copies); the
    # read-only user atts stay as the caller's mappings.
    workflow_atts: list[dict] = []
    user_atts: list[Mapping[str, Any]] = []
    for att in attachments or []:
        src = att.get("source")
        if isinstance(src, str) and src.startswith("workflow:"):
            workflow_atts.append(dict(att))
        else:
            user_atts.append(att)

    rejected_workflow_atts: list[dict] = []

    async with get_db() as db:
        # BEGIN IMMEDIATE so the workflow batch's read-then-evict-then-
        # insert sequence executes under the write lock alongside the
        # message INSERT. The cache helper enforces this -- it raises
        # if its conn is not already in a transaction.
        await db.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).isoformat()
        try:
            cur = await db.execute(
                "INSERT INTO messages (conversation_id, role, content, writer_draft, turn_index, parent_id, progressive_fields, created_at, speaker_member_id, exchange_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cid,
                    role,
                    content,
                    writer_draft,
                    turn_index,
                    parent_id,
                    json.dumps(progressive_fields or {}),
                    now,
                    speaker_member_id,
                    exchange_id,
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
        # Persist workflow attachments through the registered persister
        # (see register_workflow_attachment_persister) so the database layer
        # never imports up into backend.workflows.
        if workflow_atts:
            if _workflow_attachment_persister is None:
                raise RuntimeError(
                    "workflow attachments supplied to add_message but no persister is "
                    "registered -- import backend.workflows before producing them"
                )
            _, rejected_workflow_atts = await _workflow_attachment_persister(message_id, workflow_atts, db=db)
        if advance_leaf:
            await db.execute(
                "UPDATE conversations SET updated_at = ?, active_leaf_id = ? WHERE id = ?",
                (now, message_id, cid),
            )
        else:
            await db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, cid))
        await db.commit()

    return message_id, rejected_workflow_atts


async def get_user_attachments_for_message(message_id: int) -> list[UserAttachmentRow]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, mime_type, data_b64, filename, size, created_at "
                "FROM user_attachments WHERE message_id = ? ORDER BY id",
                (message_id,),
            )
        )
        return [cast(UserAttachmentRow, dict(r)) for r in rows]


async def get_workflow_attachments_for_message(message_id: int) -> list[WorkflowAttachmentRowBase]:
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
        return [cast(WorkflowAttachmentRowBase, dict(r)) for r in rows]


async def update_message_content(msg_id: int, content: str) -> None:
    """Update the content of an existing message."""
    async with get_db() as db:
        await db.execute("UPDATE messages SET content = ? WHERE id = ?", (content, msg_id))
        await db.commit()


async def clear_writer_draft(msg_id: int) -> None:
    """Drop the retained pre-editor Writer draft for one message.

    Called when a human rewrites the row by hand: the retained draft then
    describes text that no longer exists, and the on-demand prose rewriter —
    which prefers the draft over the saved content — would restore it over the
    edit and call that a rewrite. Cleared rather than replaced with the edit,
    because "there is no pre-editor draft for this row" is the true statement;
    the rewriter's fallback then works from the saved text.
    """
    async with get_db() as db:
        await db.execute("UPDATE messages SET writer_draft = NULL WHERE id = ?", (msg_id,))
        await db.commit()


async def get_message_by_id(msg_id: int) -> MessageRow | None:
    """Fetch a single message by its primary key.

    NOTE: unlike get_path_to_leaf(), this does not JSON-decode
    ``progressive_fields``; it stays the raw string at runtime even though
    ``MessageRow`` types it as the decoded dict. See MessageRow's docstring.
    """
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,)))
        return cast(MessageRow, dict(rows[0])) if rows else None


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
    return await _get_workflow_slot("messages", "id", message_id, workflow_id)


async def set_workflow_message_state(message_id: int, workflow_id: str, payload: dict | None) -> None:
    """Update one workflow attachment state atomically."""
    await _set_workflow_slot("messages", "id", message_id, workflow_id, payload)


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
            WITH RECURSIVE subtree(id, depth) AS (
                SELECT id, 0 FROM messages WHERE conversation_id = ? AND {sibling_cond}
                UNION ALL
                SELECT m.id, s.depth + 1 FROM messages m
                INNER JOIN subtree s ON m.parent_id = s.id
                WHERE {_SAME_CONVERSATION}
            )
            SELECT id, depth FROM subtree
        """,
                (cid, *sibling_params, cid),
            )
        )
        subtree = [(int(r["depth"]), int(r["id"])) for r in desc_rows]
        deleted_ids = {mid for _, mid in subtree}

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

        # Deepest-first, one statement per depth level. Order is the whole point:
        # ``messages.parent_id`` cascades, so deleting a parent before its child
        # makes SQLite walk the subtree itself, recursing once per level of the
        # chat -- 6.3 s of the 6.4 s a 3,000-message delete used to take. Leaves
        # first means every cascade lookup finds nothing, and
        # ``idx_messages_parent`` makes that lookup a seek instead of a scan.
        # Rows at one depth are never ancestors of each other, so order within a
        # level is free -- which is what lets each level go out in one statement.
        for level_ids in _levels_deepest_first(subtree):
            for chunk in _chunked(level_ids, _SQL_PARAM_CHUNK):
                placeholders = ",".join("?" * len(chunk))
                await db.execute(
                    f"DELETE FROM messages WHERE id IN ({placeholders})",  # nosec B608 — placeholders is only '?' chars, ids are parameterised
                    chunk,
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


async def get_message_delete_preview(cid: str, msg_id: int) -> dict[str, int] | None:
    """Count the sibling subtrees removed by ``delete_message_with_descendants``."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT parent_id FROM messages WHERE id = ? AND conversation_id = ?",
                (msg_id, cid),
            )
        )
        if not rows:
            return None
        parent_id = rows[0]["parent_id"]
        if parent_id is None:
            root_clause = "m.parent_id IS NULL"
            params: tuple[Any, ...] = (cid, cid)
        else:
            root_clause = "m.parent_id = ?"
            params = (cid, parent_id, cid)
        counts = list(
            await db.execute_fetchall(
                f"""
                WITH RECURSIVE subtree(id, role) AS (
                    SELECT m.id, m.role FROM messages m
                    WHERE m.conversation_id = ? AND {root_clause}
                    UNION ALL
                    SELECT m.id, m.role FROM messages m
                    JOIN subtree s ON m.parent_id = s.id
                    WHERE {_SAME_CONVERSATION}
                )
                SELECT COUNT(*) AS message_count,
                       SUM(CASE WHEN role = 'assistant' THEN 1 ELSE 0 END) AS assistant_count
                FROM subtree
                """,
                params,
            )
        )
        row = counts[0]
        return {"message_count": int(row["message_count"]), "assistant_count": int(row["assistant_count"] or 0)}
