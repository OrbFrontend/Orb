"""Interactive fragments, each a row of one profile. Without a ``profile_id`` every function acts on the active profile."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

import aiosqlite

from ...core import DECISION_COLUMNS, DECISION_FIELD_TYPE, STATE_COLUMNS, fragment_stage, is_director_fragment
from ..connection import build_set_clause, check_expected, get_db, immediate_tx, select_rows
from ..models import InteractiveFragmentRow
from .profiles import get_active_profile_id

# Decode decision JSON at the read boundary; malformed values remain invalid definitions instead of failing the fragment list.
_DECISION_JSON_COLUMNS = ("decision_criteria", "decision_outputs")

# Authoring columns a create or update may write, besides the decision ones.
_BASE_WRITE_COLUMNS = (
    "label",
    "description",
    "field_type",
    "required",
    "enabled",
    "injection_label",
    "sort_order",
    "cooldown_turns",
    "gate_question",
    "gate_replies",
    *STATE_COLUMNS,
)


# Binds the given sort_order, then the profile id.
_LAST_SLOT = "COALESCE(?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM interactive_fragments WHERE profile_id = ?))"


class InteractiveFragmentReorderLaneMismatch(ValueError):
    """A reorder named fragments from more than one stage."""


def _decoded(row: Any) -> InteractiveFragmentRow:
    fragment = dict(row)
    for column in _DECISION_JSON_COLUMNS:
        raw = fragment.get(column)
        if isinstance(raw, str):
            try:
                fragment[column] = json.loads(raw) if raw.strip() else None
            except ValueError:
                fragment[column] = None
    return cast(InteractiveFragmentRow, fragment)


def _encoded_decision_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Serialize the JSON-valued decision fields present in *data*.

    Callers pass the in-memory shape (a mapping); the column holds text. Absent keys stay absent so a partial update does not
    blank a field it never mentioned.
    """
    out = dict(data)
    for column in _DECISION_JSON_COLUMNS:
        if column in out and out[column] is not None and not isinstance(out[column], str):
            out[column] = json.dumps(out[column], ensure_ascii=False)
    return out


async def get_interactive_fragments(profile_id: str | None = None) -> list[InteractiveFragmentRow]:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows(
        "SELECT * FROM interactive_fragments WHERE profile_id = ? ORDER BY sort_order ASC, id ASC", (profile_id,)
    )
    return [_decoded(r) for r in rows]


async def get_interactive_fragment(fid: str, profile_id: str | None = None) -> InteractiveFragmentRow | None:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows("SELECT * FROM interactive_fragments WHERE profile_id = ? AND id = ?", (profile_id, fid))
    return _decoded(rows[0]) if rows else None


async def create_interactive_fragment(data: dict, profile_id: str | None = None) -> InteractiveFragmentRow | None:
    """Insert a fragment; without a ``sort_order`` it goes last, or a decision just ahead of the first Director fragment."""
    profile_id = profile_id or await get_active_profile_id()
    payload = _encoded_decision_values(data)
    columns = ("profile_id", "id", *_BASE_WRITE_COLUMNS, *DECISION_COLUMNS)
    values = (
        profile_id,
        payload["id"],
        payload["label"],
        payload["description"],
        payload.get("field_type", "string"),
        1 if payload.get("required", False) else 0,
        1 if payload.get("enabled", True) else 0,
        payload["injection_label"],
        payload.get("sort_order"),
        profile_id,
        payload.get("cooldown_turns", 0),
        # An explicit NULL would bypass the column default and violate NOT NULL.
        payload.get("gate_question") or "",
        payload.get("gate_replies") or 0,
        *(payload.get(column) for column in STATE_COLUMNS),
        *(payload.get(column) for column in DECISION_COLUMNS),
    )
    async with immediate_tx() as db:
        await db.execute(
            f"INSERT INTO interactive_fragments ({', '.join(columns)}) "  # nosec B608 -- columns from module literals
            f"VALUES ({', '.join(_LAST_SLOT if column == 'sort_order' else '?' for column in columns)})",
            values,
        )
        if payload.get("sort_order") is None and payload.get("field_type") == DECISION_FIELD_TYPE:
            await _put_ahead_of_director(db, profile_id, str(payload["id"]))
    return await get_interactive_fragment(payload["id"], profile_id)


async def _put_ahead_of_director(db: aiosqlite.Connection, profile_id: str, fid: str) -> None:
    """Move decision *fid* just ahead of the first Director fragment, where it asks the Judge before the Director runs."""
    rows = await db.execute_fetchall(
        "SELECT id, field_type FROM interactive_fragments WHERE profile_id = ? ORDER BY sort_order, id", (profile_id,)
    )
    others = [row for row in rows if fragment_stage(row["field_type"]) == "director" and row["id"] != fid]
    first = next((i for i, row in enumerate(others) if is_director_fragment(row["field_type"])), None)
    if first is None:
        return
    ids = [str(row["id"]) for row in others]
    ids.insert(first, fid)
    await _reorder(db, profile_id, ids)


async def update_interactive_fragment(
    fid: str, data: dict, *, expected: Mapping[str, Any] | None = None, profile_id: str | None = None
) -> InteractiveFragmentRow | None:
    """Update the fragment's authoring columns; ``None`` when there is no such fragment.

    With *expected*, the named columns are compared and the update written in one transaction, raising ``StaleWrite`` if any
    changed since the caller read them; a decision JSON column compares in its decoded shape.
    """
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        owner = {"profile_id": profile_id}
        if expected is not None and not await check_expected(db, "interactive_fragments", fid, expected, owner=owner):
            return None
        sets, vals = build_set_clause([*_BASE_WRITE_COLUMNS, *DECISION_COLUMNS], _encoded_decision_values(data))
        if sets:
            vals.extend((profile_id, fid))
            await db.execute(
                f"UPDATE interactive_fragments SET {', '.join(sets)} WHERE profile_id = ? AND id = ?",  # nosec B608 -- cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        elif expected is not None:
            # Ends the transaction the comparison opened.
            await db.commit()
        return await get_interactive_fragment(fid, profile_id)


async def reorder_interactive_fragments(ids: Sequence[str], profile_id: str | None = None) -> dict[str, int] | None:
    """Put one stage's fragments in the order of *ids*; every fragment's slot by id, or ``None`` when an id is gone.

    The named fragments trade the places they hold in the full list, so no other fragment moves, and tied slots are spread
    apart in the order they ran. Read and written under one write lock; a refused batch changes nothing.
    """
    profile_id = profile_id or await get_active_profile_id()
    async with immediate_tx() as db:
        return await _reorder(db, profile_id, ids)


async def _reorder(db: aiosqlite.Connection, profile_id: str, ids: Sequence[str]) -> dict[str, int] | None:
    rows = list(
        await db.execute_fetchall(
            "SELECT id, field_type, sort_order FROM interactive_fragments WHERE profile_id = ? ORDER BY sort_order, id",
            (profile_id,),
        )
    )
    stage_by_id = {str(row["id"]): fragment_stage(row["field_type"]) for row in rows}
    if any(fid not in stage_by_id for fid in ids):
        return None
    if len({stage_by_id[fid] for fid in ids}) > 1:
        raise InteractiveFragmentReorderLaneMismatch("Fragments must belong to the same stage")
    named, asked = set(ids), iter(ids)
    order = [next(asked) if row["id"] in named else str(row["id"]) for row in rows]
    slots = [int(row["sort_order"]) for row in rows]
    for index in range(1, len(slots)):
        slots[index] = max(slots[index], slots[index - 1] + 1)
    placed = dict(zip(order, slots))
    held = {str(row["id"]): row["sort_order"] for row in rows}
    await db.executemany(
        "UPDATE interactive_fragments SET sort_order = ? WHERE profile_id = ? AND id = ?",
        [(slot, profile_id, fid) for fid, slot in placed.items() if held[fid] != slot],
    )
    return placed


async def delete_interactive_fragment(fid: str, profile_id: str | None = None) -> bool:
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        cur = await db.execute("DELETE FROM interactive_fragments WHERE profile_id = ? AND id = ?", (profile_id, fid))
        await db.commit()
        return cur.rowcount > 0
