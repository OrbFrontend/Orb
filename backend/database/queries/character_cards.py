from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

import aiosqlite

from ...core import has_inline_macros, resolve_inline
from ..connection import _build_set_clause, get_db
from ..models import CharacterCardRow, InteractiveFragmentRow, MoodFragmentRow


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
                "SELECT c.id, c.name, c.creator_notes, c.tags, c.creator, c.source_format, c.created_at, c.updated_at, c.avatar_mime, c.world_id, c.persona_lock_id, "
                "EXISTS(SELECT 1 FROM character_expressions e WHERE e.character_card_id = c.id) AS has_expressions "
                "FROM character_cards c ORDER BY c.updated_at DESC"
            )
        )
        result: list[CharacterCardRow] = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"]) if d["tags"] else []
            d["has_avatar"] = d["avatar_mime"] is not None
            del d["avatar_mime"]
            d["has_expressions"] = bool(d["has_expressions"])
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
                "character_version, alternate_greetings, avatar_mime, source_format, world_id, persona_lock_id, "
                "extensions, created_at, updated_at"
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
        d["extensions"] = json.loads(d["extensions"]) if d.get("extensions") else {}
        d["has_avatar"] = d.get("avatar_mime") is not None
        return cast(CharacterCardRow, d)


# Server-side mirror of frontend FRAGMENT_ID_REGEX, plus a length cap: card
# fragment ids become LLM tool-schema property names, and some backends
# enforce a strict charset/length on those.
_CARD_FRAGMENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_INTERACTIVE_FIELD_TYPES = {"string", "array", "progressive", "feedback", "direction_note"}


def _card_fragment_entries(raw: Any) -> list[dict]:
    """Filter a raw fragments list down to well-formed, enabled, unique entries."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw[:50]:
        if not isinstance(entry, dict):
            continue
        fid, label = entry.get("id"), entry.get("label")
        if not (isinstance(fid, str) and _CARD_FRAGMENT_ID.match(fid)):
            continue
        if not (isinstance(label, str) and label.strip()):
            continue
        if fid in seen or not entry.get("enabled", True):
            continue
        seen.add(fid)
        out.append(entry)
    return out


def _text(entry: Mapping[str, Any], key: str, default: str = "") -> str:
    v = entry.get(key, default)
    return v if isinstance(v, str) else default


def card_embedded_fragments(
    card: Mapping[str, Any] | None,
) -> tuple[list[MoodFragmentRow], list[InteractiveFragmentRow]]:
    """Decode a card's ``extensions.orb.fragments`` into fragment-row shapes.

    This is the trust boundary for card-embedded fragments: cards come from
    arbitrary imported PNGs, so every nesting level is type-checked, ids are
    validated, unknown enum values fall back to safe defaults, and malformed,
    disabled, or duplicate (first wins) entries are skipped. Callers merge the
    result into the global fragment lists; on id collision the global wins so
    a card can never hijack a user-configured fragment.
    """
    ext = (card or {}).get("extensions")
    orb = ext.get("orb") if isinstance(ext, dict) else None
    frags = orb.get("fragments") if isinstance(orb, dict) else None
    if not isinstance(frags, dict):
        return [], []

    moods: list[MoodFragmentRow] = []
    for entry in _card_fragment_entries(frags.get("mood")):
        moods.append(
            cast(
                MoodFragmentRow,
                {
                    "id": entry["id"],
                    "label": entry["label"],
                    "description": _text(entry, "description"),
                    "prompt_text": _text(entry, "prompt_text"),
                    "negative_prompt": _text(entry, "negative_prompt"),
                    "enabled": 1,
                },
            )
        )

    interactive: list[InteractiveFragmentRow] = []
    for i, entry in enumerate(_card_fragment_entries(frags.get("interactive"))):
        field_type = _text(entry, "field_type", "string")
        timing = _text(entry, "direction_note_timing", "post_turn")
        interactive.append(
            cast(
                InteractiveFragmentRow,
                {
                    "id": entry["id"],
                    "label": entry["label"],
                    "description": _text(entry, "description"),
                    "field_type": field_type if field_type in _INTERACTIVE_FIELD_TYPES else "string",
                    "required": int(bool(entry.get("required"))),
                    "enabled": 1,
                    "injection_label": _text(entry, "injection_label") or entry["label"],
                    # Array order in the card is authoritative; the offset keeps
                    # card fragments after globals on any sort_order re-sort.
                    "sort_order": 10_000 + i,
                    "direction_note_timing": timing if timing in ("pre_writer", "post_turn") else "post_turn",
                },
            )
        )

    return moods, interactive


async def create_character_card(data: dict) -> CharacterCardRow:
    async with get_db() as db:
        now = datetime.now(UTC).isoformat()
        try:
            await db.execute(
                """INSERT INTO character_cards
                   (id, name, description, personality, scenario, first_mes, mes_example,
                    creator_notes, system_prompt, post_history_instructions, tags, creator,
                    character_version, alternate_greetings, avatar_b64, avatar_mime,
                    source_format, world_id, extensions, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    json.dumps(data["extensions"]) if data.get("extensions") else None,
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
    switch_to_branch. Content is stored macro-resolved; greetings with inline
    macros keep their raw template in the per-message "macros" slot so they can
    re-roll until the conversation's first user message (see
    :func:`reroll_unfrozen_greetings`). Returns the number of greetings inserted.
    """
    if not alternate_greetings:
        return 0
    async with get_db() as db:
        now = datetime.now(UTC).isoformat()
        count = 0
        for greeting in alternate_greetings:
            if greeting and greeting.strip():
                count += 1
                raw = greeting.strip()
                workflow_state = json.dumps({"macros": {"template": raw}}) if has_inline_macros(raw) else None
                await db.execute(
                    "INSERT INTO messages "
                    "(conversation_id, role, content, turn_index, parent_id, created_at, workflow_state) "
                    "VALUES (?, ?, ?, 0, NULL, ?, ?)",
                    (cid, "assistant", resolve_inline(raw), now, workflow_state),
                )
        if count:
            await db.commit()
        return count


async def reroll_unfrozen_greetings(cid: str) -> None:
    """Re-roll inline macros in the conversation's root greetings while unfrozen.

    A greeting row stores macro-resolved text in ``content`` and its raw
    template in the "macros" per-message slot. Until the conversation has a
    user message, every fetch may re-resolve freely; once one exists the
    NOT EXISTS guard matches nothing and the last-displayed resolution stays
    fixed forever (display, DB, and the first turn's prompt all read the same
    bytes). Rows without a stashed template (no macros, or copies made by
    checkpoint — which drops workflow_state) are left untouched.
    """
    async with get_db() as db:
        greetings = list(
            await db.execute_fetchall(
                "SELECT id, json_extract(workflow_state, '$.macros.template') AS template "
                "FROM messages "
                "WHERE conversation_id = ? AND parent_id IS NULL "
                "  AND json_extract(workflow_state, '$.macros.template') IS NOT NULL "
                "  AND NOT EXISTS (SELECT 1 FROM messages WHERE conversation_id = ? AND role = 'user')",
                (cid, cid),
            )
        )
        for row in greetings:
            await db.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (resolve_inline(row["template"]), row["id"]),
            )
        if greetings:
            await db.commit()


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
            "persona_lock_id",
        ]
        sets, vals = _build_set_clause(allowed, data)
        # JSON fields
        if "tags" in data:
            sets.append("tags = ?")
            vals.append(json.dumps(data["tags"]))
        if "alternate_greetings" in data:
            sets.append("alternate_greetings = ?")
            vals.append(json.dumps(data["alternate_greetings"]))
        if "extensions" in data:
            sets.append("extensions = ?")
            vals.append(json.dumps(data["extensions"]))
        # Avatar
        if "avatar_b64" in data:
            sets.append("avatar_b64 = ?")
            vals.append(data["avatar_b64"])
            sets.append("avatar_mime = ?")
            vals.append(data.get("avatar_mime"))

        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(UTC).isoformat())
            vals.append(card_id)
            await db.execute(
                f"UPDATE character_cards SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        return await get_character_card(card_id)


async def set_public_profile(card_id: str, appearance: str, role: str) -> CharacterCardRow | None:
    """Merge only ``extensions.orb.public_profile`` and preserve every sibling key."""
    card = await get_character_card(card_id)
    if not card:
        return None
    extensions = dict(card.get("extensions") or {})
    orb = dict(extensions.get("orb") or {}) if isinstance(extensions.get("orb"), dict) else {}
    orb["public_profile"] = {"appearance": appearance, "role": role}
    extensions["orb"] = orb
    return await update_character_card(card_id, {"extensions": extensions})


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
            await db.execute(
                """DELETE FROM conversations
                   WHERE character_card_id = ?
                      OR id IN (SELECT conversation_id FROM group_members WHERE character_card_id = ?)""",
                (card_id, card_id),
            )
        # When keeping conversations, character_card_id is intentionally left as-is.
        # The dangling reference acts as a pending-relink marker: re-importing the
        # same card (which produces the same stable ID) restores the association
        # automatically. resolve_char_context() handles a missing card gracefully.
        cur = await db.execute("DELETE FROM character_cards WHERE id = ?", (card_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_character_usage(card_id: str) -> dict[str, int]:
    """Return solo and active/historical group conversation usage counts."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                """SELECT
                     (SELECT COUNT(*) FROM conversations WHERE character_card_id = ?) AS solo,
                     (SELECT COUNT(DISTINCT conversation_id) FROM group_members
                      WHERE character_card_id = ? AND active = 1) AS active_groups,
                     (SELECT COUNT(DISTINCT conversation_id) FROM group_members
                      WHERE character_card_id = ? AND active = 0) AS historical_groups""",
                (card_id, card_id, card_id),
            )
        )
    row = rows[0]
    return {"solo": int(row[0]), "active_groups": int(row[1]), "historical_groups": int(row[2])}


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

    Caller must hold backend.core.locks.workflow_character_state_lock(character_id,
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
