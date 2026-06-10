from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, Mapping, cast

import aiosqlite

from ..connection import _build_set_clause, get_db
from ..models import CharacterCardRow


async def list_character_cards() -> list[CharacterCardRow]:
    # Projects only the columns the library sidebar/list consumes. The heavy text
    # bodies (description, personality, scenario, first_mes, system_prompt) are
    # deliberately excluded: nothing in the list path reads them, and shipping
    # them for every card turns a large library (~2000 cards) into a multi-MB
    # payload that the client must transfer, JSON-parse, and hold resident on
    # every refresh. The edit modal lazy-loads the full card via
    # get_character_card() when needed.
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, name, creator_notes, tags, creator, source_format, created_at, updated_at, avatar_mime, world_id FROM character_cards ORDER BY updated_at DESC"
            )
        )
        result: list[CharacterCardRow] = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"]) if d["tags"] else []
            d["has_avatar"] = d["avatar_mime"] is not None
            del d["avatar_mime"]
            result.append(cast(CharacterCardRow, d))
        return result


async def get_character_card(card_id: str, include_avatar: bool = False) -> CharacterCardRow | None:
    async with get_db() as db:
        cols = (
            "*"
            if include_avatar
            else (
                "id, name, description, personality, scenario, first_mes, mes_example, "
                "creator_notes, system_prompt, post_history_instructions, tags, creator, "
                "character_version, alternate_greetings, avatar_mime, source_format, world_id, created_at, updated_at"
            )
        )
        rows = list(
            await db.execute_fetchall(
                f"SELECT {cols} FROM character_cards WHERE id = ?",
                (card_id,),  # nosec B608 — cols is a hardcoded literal, not user input
            )
        )
        if not rows:
            return None
        d = dict(rows[0])
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["alternate_greetings"] = json.loads(d["alternate_greetings"]) if d.get("alternate_greetings") else []
        d["has_avatar"] = d.get("avatar_mime") is not None
        return cast(CharacterCardRow, d)


async def create_character_card(data: dict) -> CharacterCardRow:
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        try:
            await db.execute(
                """INSERT INTO character_cards
                   (id, name, description, personality, scenario, first_mes, mes_example,
                    creator_notes, system_prompt, post_history_instructions, tags, creator,
                    character_version, alternate_greetings, avatar_b64, avatar_mime,
                    source_format, world_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["id"],
                    data["name"],
                    data.get("description", ""),
                    data.get("personality", ""),
                    data.get("scenario", ""),
                    data.get("first_mes", ""),
                    data.get("mes_example", ""),
                    data.get("creator_notes", ""),
                    data.get("system_prompt", ""),
                    data.get("post_history_instructions", ""),
                    json.dumps(data.get("tags", [])),
                    data.get("creator", ""),
                    data.get("character_version", ""),
                    json.dumps(data.get("alternate_greetings", [])),
                    data.get("avatar_b64"),
                    data.get("avatar_mime"),
                    data.get("source_format", "manual"),
                    data.get("world_id"),
                    now,
                    now,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ValueError(f"Character card with id {data['id']} already exists") from exc
        await db.commit()
        result = await get_character_card(data["id"])
        assert result is not None
        return result


async def insert_alternate_greeting_swipes(cid: str, alternate_greetings: list[str]) -> int:
    """Insert alternate greetings as sibling root messages (turn_index=0, parent_id=NULL).

    These become branch siblings of the primary greeting and are navigable via
    switch_to_branch. Returns the number of greetings inserted.
    """
    if not alternate_greetings:
        return 0
    async with get_db() as db:
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for greeting in alternate_greetings:
            if greeting and greeting.strip():
                count += 1
                await db.execute(
                    "INSERT INTO messages "
                    "(conversation_id, role, content, turn_index, parent_id, created_at) "
                    "VALUES (?, ?, ?, 0, NULL, ?)",
                    (cid, "assistant", greeting.strip(), now),
                )
        if count:
            await db.commit()
        return count


async def update_character_card(card_id: str, data: dict) -> CharacterCardRow | None:
    async with get_db() as db:
        allowed = [
            "name",
            "description",
            "personality",
            "scenario",
            "first_mes",
            "mes_example",
            "creator_notes",
            "system_prompt",
            "post_history_instructions",
            "creator",
            "character_version",
            "world_id",
        ]
        sets, vals = _build_set_clause(allowed, data)
        # JSON fields
        if "tags" in data:
            sets.append("tags = ?")
            vals.append(json.dumps(data["tags"]))
        if "alternate_greetings" in data:
            sets.append("alternate_greetings = ?")
            vals.append(json.dumps(data["alternate_greetings"]))
        # Avatar
        if "avatar_b64" in data:
            sets.append("avatar_b64 = ?")
            vals.append(data["avatar_b64"])
            sets.append("avatar_mime = ?")
            vals.append(data.get("avatar_mime"))

        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(card_id)
            await db.execute(
                f"UPDATE character_cards SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_character_card(card_id)


async def sync_conversations_for_card(card_id: str, card: Mapping[str, Any], old_name: str | None = None) -> None:
    """Propagate mutable card fields to all conversations linked to this card.

    Only syncs fields that are denormalised onto the conversation row and
    affect prompt-building at runtime. first_mes is excluded because it has
    already been materialised as a message in the conversation tree.

    If ``old_name`` is provided, conversation titles that still match the old
    name are updated to the new name so they don't become stale.
    """
    async with get_db() as db:
        await db.execute(
            """UPDATE conversations
               SET character_name = ?,
                   character_scenario = ?,
                   post_history_instructions = ?
               WHERE character_card_id = ?""",
            (
                card.get("name", ""),
                card.get("scenario", ""),
                card.get("post_history_instructions", ""),
                card_id,
            ),
        )
        if old_name is not None:
            await db.execute(
                """UPDATE conversations
                   SET title = ?
                   WHERE character_card_id = ? AND title = ?
                     AND (SELECT COUNT(*) FROM messages WHERE conversation_id = conversations.id) <= 1""",
                (card.get("name", ""), card_id, old_name),
            )
        await db.commit()


async def delete_character_card(card_id: str, delete_conversations: bool = False) -> bool:
    async with get_db() as db:
        if delete_conversations:
            await db.execute("DELETE FROM conversations WHERE character_card_id = ?", (card_id,))
        # When keeping conversations, character_card_id is intentionally left as-is.
        # The dangling reference acts as a pending-relink marker: re-importing the
        # same card (which produces the same stable ID) restores the association
        # automatically. resolve_char_context() handles a missing card gracefully.
        cur = await db.execute("DELETE FROM character_cards WHERE id = ?", (card_id,))
        await db.commit()
        return cur.rowcount > 0


async def resolve_char_context(
    conv: Mapping[str, Any],
    settings: Mapping[str, Any],
    shared_key: str = "shared_system_prompt",
    card: CharacterCardRow | None = None,
) -> tuple[str, str, str]:
    """Resolve the effective system prompt, persona, and example messages.

    shared_system_prompt and the model-specific system_prompt are concatenated
    (shared first); a character card's own system_prompt, when present and not
    disabled by the prevent_prompt_overrides setting, replaces that combined
    result entirely rather than appending to it.
    """
    # Combine shared (global) + model-specific system prompts
    shared = settings.get(shared_key, "")
    model_specific = settings.get("system_prompt", "")

    if shared and model_specific:
        system_prompt = f"{shared}\n\n{model_specific}"
    else:
        system_prompt = shared or model_specific

    char_persona, mes_example = "", ""
    if card is None and (card_id := conv.get("character_card_id")):
        card = await get_character_card(card_id)
    if card:
        char_persona = "\n\n".join(filter(None, [card.get("description", ""), card.get("personality", "")]))
        mes_example = card.get("mes_example", "")
        card_system_prompt = card.get("system_prompt")
        if card_system_prompt and not settings.get("prevent_prompt_overrides"):
            system_prompt = card_system_prompt
    return system_prompt, char_persona, mes_example


async def get_character_avatar(card_id: str) -> tuple[bytes, str] | None:
    """Returns (image_bytes, mime_type) or None."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT avatar_b64, avatar_mime FROM character_cards WHERE id = ?",
                (card_id,),
            )
        )
        if not rows or not rows[0]["avatar_b64"]:
            return None
        return base64.b64decode(rows[0]["avatar_b64"]), rows[0]["avatar_mime"]


async def get_workflow_character_state(character_id: str, workflow_id: str) -> dict | None:
    """Return the workflow's slot on this character, or None if card missing or slot empty."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT json_extract(workflow_state, '$.' || ?) AS slot FROM character_cards WHERE id = ?",
                (workflow_id, character_id),
            )
        )
        if not rows:
            return None
        slot = rows[0]["slot"]
        if slot is None:
            return None
        return json.loads(slot)


async def set_workflow_character_state(character_id: str, workflow_id: str, payload: dict | None) -> None:
    """Atomic per-slot write via SQLite JSON1. payload=None removes the slot;
    empty dict stores {}. No-op if card missing (UPDATE matches zero rows).

    Caller must hold backend.locks.workflow_character_state_lock(character_id,
    workflow_id) across the read-then-write the payload was computed from.
    """
    async with get_db() as db:
        if payload is None:
            await db.execute(
                "UPDATE character_cards "
                "SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) "
                "WHERE id = ?",
                (workflow_id, character_id),
            )
        else:
            await db.execute(
                "UPDATE character_cards "
                "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                "WHERE id = ?",
                (workflow_id, json.dumps(payload), character_id),
            )
        await db.commit()
