from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import cast

from ..connection import _build_set_clause, get_db, immediate_tx
from ..models import ConversationListRow, ConversationRow


async def list_conversations() -> list[ConversationListRow]:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                """
            WITH RECURSIVE active_path(conv_id, id, parent_id) AS (
                SELECT c.id, m.id, m.parent_id
                FROM conversations c
                JOIN messages m ON m.id = c.active_leaf_id
                UNION ALL
                SELECT ap.conv_id, m.id, m.parent_id
                FROM active_path ap
                JOIN messages m ON m.id = ap.parent_id
            ),
            active_counts(conv_id, cnt) AS (
                SELECT conv_id, COUNT(*) FROM active_path GROUP BY conv_id
            )
            SELECT c.*,
                   (SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS last_message_preview,
                   COALESCE((SELECT json_group_array(gm.character_card_id)
                             FROM group_members gm
                             WHERE gm.conversation_id = c.id AND gm.active = 1
                               AND gm.character_card_id IS NOT NULL), '[]') AS group_card_ids,
                   COALESCE((SELECT json_group_array(display_name)
                             FROM (
                                 SELECT gm.display_name
                                 FROM group_members gm
                                 WHERE gm.conversation_id = c.id AND gm.active = 1
                                 ORDER BY gm.sort_order, gm.id
                             )), '[]') AS group_member_names,
                   COALESCE(ac.cnt, 0) AS message_count
            FROM conversations c
            LEFT JOIN active_counts ac ON ac.conv_id = c.id
            ORDER BY max(COALESCE(c.last_accessed_at, ''), COALESCE(c.updated_at, ''), c.created_at) DESC
        """
            )
        )
        out: list[ConversationListRow] = []
        for row in rows:
            item = dict(row)
            item["group_card_ids"] = json.loads(item.get("group_card_ids") or "[]")
            item["group_member_names"] = json.loads(item.get("group_member_names") or "[]")
            out.append(cast(ConversationListRow, item))
        return out


def group_root_of(conv: ConversationRow) -> str:
    """The id of the group family *conv* belongs to.

    A root stores NULL and is its own family key, so every read of the column
    goes through here rather than repeating the fallback. Meaningless for solo
    conversations, which have no family; callers gate on ``kind`` first.
    """
    return str(conv.get("group_root_id") or conv["id"])


async def get_conversation(cid: str) -> ConversationRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (cid,)))
        return cast(ConversationRow, dict(rows[0])) if rows else None


async def create_conversation(
    cid: str,
    title: str,
    char_name: str,
    char_scenario: str,
    post_history_instructions: str = "",
    character_card_id: str | None = None,
    persona_lock_id: int | None = None,
    macro_seed: str = "",
) -> ConversationRow:
    async with get_db() as db:
        now = datetime.now(UTC).isoformat()
        await db.execute(
            """INSERT INTO conversations
               (id, title, character_card_id, character_name, character_scenario,
                post_history_instructions, persona_lock_id, macro_seed, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                title,
                character_card_id,
                char_name,
                char_scenario,
                post_history_instructions,
                persona_lock_id,
                macro_seed,
                now,
                now,
            ),
        )
        await db.execute(
            "INSERT INTO director_state (conversation_id, active_moods, keywords) VALUES (?, '[]', '[]')",
            (cid,),
        )
        await db.commit()
        result = await get_conversation(cid)
        assert result is not None
        return result


async def fork_conversation(source: ConversationRow, new_title: str) -> str:
    """Create an empty conversation seeded from ``source``'s character framing.

    Carries the title-independent identity fields (character name/scenario,
    post-history instructions, card id, persona pin) that both the Compress
    History and Checkpoint flows start a fork with, and returns the new
    conversation id.
    Messages, branches, director state and logs are *not* copied -- the caller
    appends whatever slice of the source it intends to carry.

    A group fork joins ``source``'s family rather than founding one: a
    checkpoint is a branch of the group, not a second group, and the sidebar
    reads that lineage to keep showing one entry per group.

    The copy pins ``macro_seed`` to the source's effective seed so seeded
    {{random}} picks in per-turn-rebuilt prompt fields (persona, scenario)
    stay byte-identical to the history being carried, instead of re-rolling
    under the new conversation id.
    """
    new_cid = str(uuid.uuid4())
    if source.get("kind", "solo") == "group":
        from .group_members import create_group_conversation, get_group_members

        members = await get_group_members(source["id"], include_inactive=True)
        await create_group_conversation(
            new_cid,
            new_title,
            [
                {
                    "speaker_key": m["speaker_key"],
                    "character_card_id": m.get("character_card_id"),
                    "display_name": m["display_name"],
                    "public_profile_override": m.get("public_profile_override"),
                    "card_sheet_override": m.get("card_sheet_override"),
                    "member_kind": m["member_kind"],
                    "muted": bool(m["muted"]),
                    "active": bool(m["active"]),
                }
                for m in members
            ],
            scenario=source.get("character_scenario", "") or "",
            post_history_instructions=source.get("post_history_instructions", "") or "",
            turn_mode=source.get("group_turn_mode", "director"),
            max_speakers=source.get("group_max_speakers", 3),
            context_mode=source.get("group_context_mode", "private"),
            sheet_updates=bool(source.get("group_sheet_updates")),
            persona_lock_id=source.get("persona_lock_id"),
            macro_seed=source.get("macro_seed") or source["id"],
            group_root_id=group_root_of(source),
        )
    else:
        await create_conversation(
            cid=new_cid,
            title=new_title,
            char_name=source.get("character_name", "") or "",
            char_scenario=source.get("character_scenario", "") or "",
            post_history_instructions=source.get("post_history_instructions", "") or "",
            character_card_id=source.get("character_card_id"),
            persona_lock_id=source.get("persona_lock_id"),
            macro_seed=source.get("macro_seed") or source["id"],
        )
    return new_cid


async def delete_conversation(cid: str) -> bool:
    """Delete one conversation, keeping the rest of its group family together.

    Deleting the root of a family would otherwise strand its forks: the FK
    clears their ``group_root_id`` and each one surfaces as a separate group --
    exactly the duplication the lineage exists to prevent. So the oldest
    survivor is promoted to root and the others re-pointed at it first, in one
    transaction with the delete.
    """
    async with immediate_tx() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, kind, group_root_id FROM conversations WHERE id = ?",
                (cid,),
            )
        )
        if not rows:
            return False
        conv = dict(rows[0])
        # Only a root has children to rehome; a fork's siblings point elsewhere.
        if conv["kind"] == "group" and not conv["group_root_id"]:
            children = list(
                await db.execute_fetchall(
                    "SELECT id FROM conversations WHERE group_root_id = ? ORDER BY created_at, id",
                    (cid,),
                )
            )
            if children:
                heir = children[0]["id"]
                await db.execute("UPDATE conversations SET group_root_id = NULL WHERE id = ?", (heir,))
                await db.execute(
                    "UPDATE conversations SET group_root_id = ? WHERE group_root_id = ? AND id != ?",
                    (heir, cid, heir),
                )
        cur = await db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        return cur.rowcount > 0


async def delete_group_family(root_cid: str) -> int:
    """Delete a whole group family -- the root and every fork taken from it.

    What the sidebar's × means once one row stands for the whole group. Unlike a
    character card, a group has no existence apart from its conversations, so
    there is nothing to keep behind after they go.
    """
    async with immediate_tx() as db:
        cur = await db.execute(
            "DELETE FROM conversations WHERE id = ? OR group_root_id = ?",
            (root_cid, root_cid),
        )
        return cur.rowcount


async def touch_conversation(cid: str) -> bool:
    """Mark a conversation accessed (opened/selected) — bumps last_accessed_at,
    not updated_at. updated_at means content changed; opening isn't an edit."""
    async with get_db() as db:
        now = datetime.now(UTC).isoformat()
        cur = await db.execute("UPDATE conversations SET last_accessed_at = ? WHERE id = ?", (now, cid))
        await db.commit()
        return cur.rowcount > 0


async def update_conversation(cid: str, data: dict) -> ConversationRow | None:
    async with get_db() as db:
        allowed = [
            "title",
            "persona_lock_id",
            "group_turn_mode",
            "group_max_speakers",
            "group_context_mode",
            "group_sheet_updates",
            "character_scenario",
            "post_history_instructions",
        ]
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            # updated_at is the conversation's "last activity" date (shown in the
            # history modal). Pinning/changing a persona is metadata, not chat
            # activity, so a persona_lock_id-only update must not bump it.
            if any(k in data for k in allowed if k != "persona_lock_id"):
                sets.append("updated_at = ?")
                vals.append(datetime.now(UTC).isoformat())
            vals.append(cid)
            await db.execute(
                f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        return await get_conversation(cid)


async def get_workflow_state(conv_id: str, workflow_id: str) -> dict | None:
    """Return the workflow's slot, or None if conversation missing or slot empty."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT json_extract(workflow_state, '$.' || ?) AS slot FROM conversations WHERE id = ?",
                (workflow_id, conv_id),
            )
        )
        if not rows:
            return None
        slot = rows[0]["slot"]
        if slot is None:
            return None
        return json.loads(slot)


async def set_workflow_state(conv_id: str, workflow_id: str, payload: dict | None) -> None:
    """Atomic per-slot write via SQLite JSON1.

    payload=None removes the slot. Empty dict stores {}. No-op if conversation
    missing (UPDATE matches zero rows).

    Caller must hold ``backend.core.locks.workflow_state_lock(conv_id, workflow_id)``
    across the read-then-write the payload was computed from. Acquisition
    sites: ``backend.api.routes.workflows.api_trigger_workflow`` and the pre/post pipeline
    hook loops in ``backend.pipeline.workflow_bridge``. Direct use outside those paths
    re-introduces the read-modify-write clobber.
    """
    async with get_db() as db:
        if payload is None:
            await db.execute(
                "UPDATE conversations SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) WHERE id = ?",
                (workflow_id, conv_id),
            )
        else:
            await db.execute(
                "UPDATE conversations "
                "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                "WHERE id = ?",
                (workflow_id, json.dumps(payload), conv_id),
            )
        await db.commit()
