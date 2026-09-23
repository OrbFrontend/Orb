from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from ...core import DECISION_COLUMNS
from ..connection import _build_set_clause, get_db, immediate_tx
from ..models import InteractiveFragmentRow

_EDITOR_LANE_FIELD_TYPES = frozenset(("feedback", "post_processing"))

# Decode decision JSON at the read boundary; malformed values remain invalid
# definitions instead of failing the fragment list.
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
    "direction_note_timing",
    "cooldown_turns",
)


class InteractiveFragmentReorderLaneMismatch(ValueError):
    """A reorder attempted to mix the Director and Editor priority lanes."""


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

    Callers pass the in-memory shape (a mapping); the column holds text. Absent
    keys stay absent so a partial update does not blank a field it never
    mentioned.
    """
    out = dict(data)
    for column in _DECISION_JSON_COLUMNS:
        if column in out and out[column] is not None and not isinstance(out[column], str):
            out[column] = json.dumps(out[column], ensure_ascii=False)
    return out


async def get_interactive_fragments() -> list[InteractiveFragmentRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM interactive_fragments ORDER BY sort_order ASC, label ASC"))
        return [_decoded(r) for r in rows]


async def get_interactive_fragment(fid: str) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM interactive_fragments WHERE id = ?", (fid,)))
        return _decoded(rows[0]) if rows else None


async def create_interactive_fragment(data: dict) -> InteractiveFragmentRow | None:
    payload = _encoded_decision_values(data)
    columns = ("id", *_BASE_WRITE_COLUMNS, *DECISION_COLUMNS)
    values = (
        payload["id"],
        payload["label"],
        payload["description"],
        payload.get("field_type", "string"),
        1 if payload.get("required", False) else 0,
        1 if payload.get("enabled", True) else 0,
        payload["injection_label"],
        payload.get("sort_order", 0),
        payload.get("direction_note_timing", "post_turn"),
        payload.get("cooldown_turns", 0),
        *(payload.get(column) for column in DECISION_COLUMNS),
    )
    async with get_db() as db:
        await db.execute(
            f"INSERT INTO interactive_fragments ({', '.join(columns)}) "  # nosec B608 — columns from module literals
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        await db.commit()
        return await get_interactive_fragment(payload["id"])


async def update_interactive_fragment(fid: str, data: dict) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        sets, vals = _build_set_clause([*_BASE_WRITE_COLUMNS, *DECISION_COLUMNS], _encoded_decision_values(data))
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE interactive_fragments SET {', '.join(sets)} WHERE id = ?",  # nosec B608 — cols from a hardcoded allowlist, values parameterised
                vals,
            )
            await db.commit()
        return await get_interactive_fragment(fid)


async def reorder_interactive_fragments(items: list[tuple[str, int]]) -> bool:
    """Atomically update a lane's existing fragment-priority slots.

    Callers send only the fragments in the reordered lane. All ids are checked
    while holding SQLite's write lock, so a stale, mixed-lane, or malformed
    batch cannot partly update priorities.
    """
    if not items:
        return True

    ids = [fid for fid, _ in items]
    placeholders = ", ".join("?" for _ in ids)
    async with immediate_tx() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT id, field_type FROM interactive_fragments WHERE id IN ({placeholders})",  # nosec B608 -- placeholder count derives solely from bound ids
                ids,
            )
        )
        if {str(row["id"]) for row in rows} != set(ids):
            return False
        lanes = {"editor" if row["field_type"] in _EDITOR_LANE_FIELD_TYPES else "director" for row in rows}
        if len(lanes) != 1:
            raise InteractiveFragmentReorderLaneMismatch("Fragments must belong to the same priority lane")
        await db.executemany(
            "UPDATE interactive_fragments SET sort_order = ? WHERE id = ?", [(order, fid) for fid, order in items]
        )
    return True


async def delete_interactive_fragment(fid: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM interactive_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
