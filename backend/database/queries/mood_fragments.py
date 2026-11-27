"""Mood fragments, each a row of one profile. Without a ``profile_id`` every function acts on the active profile."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..connection import build_set_clause, check_expected, get_db, select_rows
from ..models import MoodFragmentRow
from .active_profile import get_active_profile_id


async def get_mood_fragments(profile_id: str | None = None) -> list[MoodFragmentRow]:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows("SELECT * FROM mood_fragments WHERE profile_id = ? ORDER BY label ASC", (profile_id,))
    return [cast(MoodFragmentRow, dict(r)) for r in rows]


async def get_mood_fragment(fid: str, profile_id: str | None = None) -> MoodFragmentRow | None:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows("SELECT * FROM mood_fragments WHERE profile_id = ? AND id = ?", (profile_id, fid))
    return cast(MoodFragmentRow, dict(rows[0])) if rows else None


async def create_mood_fragment(data: dict, profile_id: str | None = None) -> MoodFragmentRow:
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        enabled = data.get("enabled", 1)
        await db.execute(
            "INSERT INTO mood_fragments (profile_id, id, label, description, prompt_text, negative_prompt, cooldown_turns, "
            "enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                profile_id,
                data["id"],
                data["label"],
                data["description"],
                data["prompt_text"],
                data.get("negative_prompt", ""),
                data.get("cooldown_turns", 0),
                enabled,
            ),
        )
        await db.commit()
        result = await get_mood_fragment(data["id"], profile_id)
        assert result is not None
        return result


async def update_mood_fragment(
    fid: str, data: dict, *, expected: Mapping[str, Any] | None = None, profile_id: str | None = None
) -> MoodFragmentRow | None:
    """Update the fragment's allowlisted fields; ``None`` when there is no such fragment.

    With *expected*, the named fields are compared and the update written in one transaction, raising ``StaleWrite`` if any
    changed since the caller read them.
    """
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        owner = {"profile_id": profile_id}
        if expected is not None and not await check_expected(db, "mood_fragments", fid, expected, owner=owner):
            return None
        allowed = ["label", "description", "prompt_text", "negative_prompt", "cooldown_turns", "enabled"]
        sets, vals = build_set_clause(allowed, data)
        if sets:
            vals.extend((profile_id, fid))
            await db.execute(
                f"UPDATE mood_fragments SET {', '.join(sets)} WHERE profile_id = ? AND id = ?",  # nosec B608 -- cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        elif expected is not None:
            # Ends the transaction the comparison opened.
            await db.commit()
        return await get_mood_fragment(fid, profile_id)


async def delete_mood_fragment(fid: str, profile_id: str | None = None) -> bool:
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        cur = await db.execute("DELETE FROM mood_fragments WHERE profile_id = ? AND id = ?", (profile_id, fid))
        await db.commit()
        return cur.rowcount > 0
