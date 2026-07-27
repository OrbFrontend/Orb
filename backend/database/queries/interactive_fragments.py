"""CRUD for ``interactive_fragments`` -- the Director's dynamic parameters.

``type_config`` is stored as JSON text and written through
:func:`_encoded_type_config`, which is the one place a caller's ``dict`` (or a
missing value) becomes column bytes. Reads return the raw string: an
extension-defined field type decodes and validates it against its own declared
instance schema, and a core type never looks at it at all, so eagerly decoding
here would only produce a shape nobody has validated yet.
"""

from __future__ import annotations

import json
from typing import Any, cast

from ..connection import _build_set_clause, get_db
from ..models import InteractiveFragmentRow


def _encoded_type_config(raw: Any) -> str:
    """Encode a fragment's ``type_config`` for storage.

    Accepts the decoded ``dict`` the API layer carries, a pre-encoded JSON
    object string, or ``None``. Anything else -- a list, a number, a string
    that is not a JSON object -- collapses to ``'{}'`` rather than being
    stored: the column's contract is "an object or nothing", and a row that
    violated it would fail validation later, at a point where the fragment can
    no longer say what it meant.
    """
    if raw is None or raw == "":
        return "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return "{}"
        return raw if isinstance(decoded, dict) else "{}"
    return "{}"


async def get_interactive_fragments() -> list[InteractiveFragmentRow]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM interactive_fragments ORDER BY sort_order ASC, label ASC"))
        return [cast(InteractiveFragmentRow, dict(r)) for r in rows]


async def get_interactive_fragment(fid: str) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT * FROM interactive_fragments WHERE id = ?", (fid,)))
        return cast(InteractiveFragmentRow, dict(rows[0])) if rows else None


async def create_interactive_fragment(data: dict) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO interactive_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order, direction_note_timing, type_config) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["label"],
                data["description"],
                data.get("field_type", "string"),
                1 if data.get("required", False) else 0,
                1 if data.get("enabled", True) else 0,
                data["injection_label"],
                data.get("sort_order", 0),
                data.get("direction_note_timing", "post_turn"),
                _encoded_type_config(data.get("type_config")),
            ),
        )
        await db.commit()
        return await get_interactive_fragment(data["id"])


async def update_interactive_fragment(fid: str, data: dict) -> InteractiveFragmentRow | None:
    async with get_db() as db:
        allowed = [
            "label",
            "description",
            "field_type",
            "required",
            "enabled",
            "injection_label",
            "sort_order",
            "direction_note_timing",
            "type_config",
        ]
        if "type_config" in data:
            data = {**data, "type_config": _encoded_type_config(data["type_config"])}
        sets, vals = _build_set_clause(allowed, data)
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE interactive_fragments SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_interactive_fragment(fid)


async def delete_interactive_fragment(fid: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM interactive_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
