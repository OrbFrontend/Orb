from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from ..connection import get_db, immediate_tx
from ..models import ConversationRow, GroupMemberRow


def allocate_speaker_key(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:24] or "speaker"
    key = base
    suffix = 2
    while key in used:
        ending = f"-{suffix}"
        key = f"{base[: 24 - len(ending)]}{ending}"
        suffix += 1
    used.add(key)
    return key


async def get_group_members(conversation_id: str, *, include_inactive: bool = False) -> list[GroupMemberRow]:
    where = "conversation_id = ?" if include_inactive else "conversation_id = ? AND active = 1"
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"SELECT * FROM group_members WHERE {where} ORDER BY sort_order, id",  # nosec B608 -- fixed clauses
            (conversation_id,),
        )
    return [cast(GroupMemberRow, dict(row)) for row in rows]


async def get_group_member(member_id: str, *, conversation_id: str | None = None) -> GroupMemberRow | None:
    sql = "SELECT * FROM group_members WHERE id = ?"
    args: tuple[Any, ...] = (member_id,)
    if conversation_id is not None:
        sql += " AND conversation_id = ?"
        args += (conversation_id,)
    async with get_db() as db:
        rows = list(await db.execute_fetchall(sql, args))
    return cast(GroupMemberRow, dict(rows[0])) if rows else None


async def create_group_conversation(
    cid: str,
    title: str,
    members: Sequence[Mapping[str, Any]],
    *,
    scenario: str = "",
    post_history_instructions: str = "",
    turn_mode: str = "director",
    max_speakers: int = 3,
    context_mode: str = "private",
    persona_lock_id: int | None = None,
    greeting: str = "",
    greeting_speaker_key: str | None = None,
    macro_seed: str = "",
    group_root_id: str | None = None,
) -> ConversationRow:
    """Create the conversation, director state, roster, and greeting atomically.

    ``group_root_id`` joins the new scene to an existing group family (see
    ``fork_conversation``); the default founds a family of its own.
    """
    active_cards = [
        str(spec["character_card_id"]) for spec in members if spec.get("character_card_id") and spec.get("active", True)
    ]
    if len(active_cards) != len(set(active_cards)):
        raise ValueError("A character card may only appear once in the active roster")
    now = datetime.now(UTC).isoformat()
    used: set[str] = set()
    created: list[tuple[str, str]] = []
    async with immediate_tx() as db:
        await db.execute(
            """INSERT INTO conversations
               (id, title, character_card_id, character_name, character_scenario,
                post_history_instructions, persona_lock_id, macro_seed, created_at, updated_at,
                kind, group_turn_mode, group_max_speakers, group_context_mode, group_root_id)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 'group', ?, ?, ?, ?)""",
            (
                cid,
                title,
                title,
                scenario,
                post_history_instructions,
                persona_lock_id,
                macro_seed,
                now,
                now,
                turn_mode,
                max_speakers,
                context_mode,
                group_root_id,
            ),
        )
        await db.execute(
            "INSERT INTO director_state (conversation_id, active_moods, keywords) VALUES (?, '[]', '[]')",
            (cid,),
        )
        for order, spec in enumerate(members):
            name = str(spec.get("display_name") or spec.get("name") or "Narrator").strip() or "Narrator"
            requested = str(spec.get("speaker_key") or "").strip().casefold()
            key = allocate_speaker_key(requested or name, used)
            member_id = str(uuid.uuid4())
            await db.execute(
                """INSERT INTO group_members
                   (id, conversation_id, speaker_key, character_card_id, display_name,
                    public_profile_override, member_kind, sort_order, muted, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    member_id,
                    cid,
                    key,
                    spec.get("character_card_id"),
                    name,
                    spec.get("public_profile_override"),
                    spec.get("member_kind", "character"),
                    order,
                    int(bool(spec.get("muted", False))),
                    int(bool(spec.get("active", True))),
                ),
            )
            created.append((key, member_id))
        if greeting.strip():
            speaker_id = next((mid for key, mid in created if key == greeting_speaker_key), None)
            if speaker_id is None:
                raise ValueError("Opening greeting speaker_key is not in the roster")
            cur = await db.execute(
                """INSERT INTO messages
                   (conversation_id, role, content, turn_index, parent_id, progressive_fields,
                    created_at, speaker_member_id, beat_id)
                   VALUES (?, 'assistant', ?, 0, NULL, '{}', ?, ?, ?)""",
                (cid, greeting.strip(), now, speaker_id, str(uuid.uuid4())),
            )
            await db.execute("UPDATE conversations SET active_leaf_id = ? WHERE id = ?", (cur.lastrowid, cid))
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (cid,)))
    return cast(ConversationRow, dict(rows[0]))


async def convert_to_group(conversation_id: str) -> GroupMemberRow:
    """Convert a solo chat and stamp all existing assistant rows in one transaction."""
    async with immediate_tx() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (conversation_id,)))
        if not rows:
            raise ValueError("Conversation not found")
        conv = dict(rows[0])
        if conv.get("kind") == "group":
            existing = list(
                await db.execute_fetchall(
                    "SELECT * FROM group_members WHERE conversation_id = ? AND active = 1 ORDER BY sort_order LIMIT 1",
                    (conversation_id,),
                )
            )
            if not existing:
                raise ValueError("Group conversation has no active members")
            return cast(GroupMemberRow, dict(existing[0]))
        name = conv.get("character_name") or "Character"
        member_id = str(uuid.uuid4())
        key = allocate_speaker_key(name, set())
        await db.execute(
            """INSERT INTO group_members
               (id, conversation_id, speaker_key, character_card_id, display_name, sort_order)
               VALUES (?, ?, ?, ?, ?, 0)""",
            (member_id, conversation_id, key, conv.get("character_card_id"), name),
        )
        await db.execute(
            "UPDATE messages SET speaker_member_id = ? WHERE conversation_id = ? AND role = 'assistant'",
            (member_id, conversation_id),
        )
        await db.execute(
            "UPDATE conversations SET kind = 'group', character_card_id = NULL, character_name = title WHERE id = ?",
            (conversation_id,),
        )
    member = await get_group_member(member_id)
    assert member is not None
    return member


async def sync_group_members(conversation_id: str, specs: Sequence[Mapping[str, Any]]) -> list[GroupMemberRow]:
    """Synchronize the active roster while tombstoning omitted historical members."""
    async with immediate_tx() as db:
        conv = list(await db.execute_fetchall("SELECT kind FROM conversations WHERE id = ?", (conversation_id,)))
        if not conv or conv[0]["kind"] != "group":
            raise ValueError("Conversation is not a group")
        existing_rows = list(
            await db.execute_fetchall("SELECT * FROM group_members WHERE conversation_id = ?", (conversation_id,))
        )
        existing = {row["id"]: dict(row) for row in existing_rows}
        used = {row["speaker_key"] for row in existing_rows}
        active_cards: set[str] = set()
        # Clear the active set first so an atomic card swap cannot trip the
        # partial unique index while the roster is only half updated.
        await db.execute("UPDATE group_members SET active = 0 WHERE conversation_id = ?", (conversation_id,))
        for order, spec in enumerate(specs):
            member_id = str(spec.get("id") or "")
            card_id = spec.get("character_card_id")
            if card_id and card_id in active_cards:
                raise ValueError("A character card may only appear once in the active roster")
            if card_id:
                active_cards.add(str(card_id))
            if member_id in existing and existing[member_id]["active"]:
                await db.execute(
                    """UPDATE group_members SET character_card_id = ?, display_name = ?,
                       public_profile_override = ?, member_kind = ?, sort_order = ?, muted = ?, active = 1
                       WHERE id = ? AND conversation_id = ?""",
                    (
                        card_id,
                        spec.get("display_name") or existing[member_id]["display_name"],
                        spec.get("public_profile_override"),
                        spec.get("member_kind", existing[member_id]["member_kind"]),
                        order,
                        int(bool(spec.get("muted", False))),
                        member_id,
                        conversation_id,
                    ),
                )
            else:
                name = str(spec.get("display_name") or spec.get("name") or "Narrator").strip() or "Narrator"
                member_id = str(uuid.uuid4())
                key = allocate_speaker_key(str(spec.get("speaker_key") or name), used)
                await db.execute(
                    """INSERT INTO group_members
                       (id, conversation_id, speaker_key, character_card_id, display_name,
                        public_profile_override, member_kind, sort_order, muted, active)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (
                        member_id,
                        conversation_id,
                        key,
                        card_id,
                        name,
                        spec.get("public_profile_override"),
                        spec.get("member_kind", "character"),
                        order,
                        int(bool(spec.get("muted", False))),
                    ),
                )
    return await get_group_members(conversation_id)
