"""Global presets: a named generation config plus the preset's prompt layout blocks.

The settings, endpoints and fragment rows are the live working copy; a preset's state (``config`` and its lane columns) is what
they held when the preset was last left. ``features.presets`` owns the switching rules.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import aiosqlite

from ...core.prompt_layout import CUSTOM_KIND, DEFAULT_LAYOUT_ID, LayoutBlock, default_layout, marker_block_id
from ..connection import get_db, immediate_tx, select_rows
from ..models import PresetConfig, PresetLanes, PresetRow, PresetState
from ..seeds import SEED_MAIN_PROMPT
from .prompt_layouts import get_prompt_blocks, insert_layout_blocks, layout_block

# The settings columns a preset carries, and those of them stored as JSON.
PRESET_SETTINGS: tuple[str, ...] = (
    "enable_agent",
    "enabled_tools",
    "length_guard_enabled",
    "length_guard_enforce",
    "length_guard_max_words",
    "length_guard_max_paragraphs",
    "agentic_lorebook_enabled",
    "director_individual_fragments",
    "prevent_prompt_overrides",
    "reasoning_enabled_passes",
    "reasoning_prefill_passes",
    "editor_audit_toggles",
)
_JSON_SETTINGS = frozenset({"enabled_tools", "reasoning_enabled_passes", "reasoning_prefill_passes", "editor_audit_toggles"})
_LIVE_LANES = ("active_endpoint_id", "agent_same_as_writer", "agent_endpoint_id", "decision_endpoint_id", "decision_model")
# The foreign-key columns holding a preset's lane picks, in PresetLanes order.
PRESET_LANES: tuple[str, ...] = (
    "writer_endpoint_id",
    "writer_model_config_id",
    "agent_endpoint_id",
    "agent_model_config_id",
    "judge_endpoint_id",
)
_COLUMNS = ", ".join(("id", "name", "config", *PRESET_LANES))
_PRESET_ORDER = "ORDER BY id != ?, name COLLATE NOCASE"


class PresetEditError(ValueError):
    """A preset edit the rules refuse; the message is safe to show."""


def _decoded(row: Any) -> PresetRow:
    return {
        "id": row["id"],
        "name": row["name"],
        "config": json.loads(row["config"] or "{}"),
        "writer_endpoint_id": row["writer_endpoint_id"],
        "writer_model_config_id": row["writer_model_config_id"],
        "agent_endpoint_id": row["agent_endpoint_id"],
        "agent_model_config_id": row["agent_model_config_id"],
        "judge_endpoint_id": row["judge_endpoint_id"],
    }


def _state_values(state: PresetState) -> tuple[Any, ...]:
    """*state* as the values of ``config`` and the PRESET_LANES columns, in that order."""
    return (json.dumps(state["config"], ensure_ascii=False), *(state[column] for column in PRESET_LANES))


def _name(name: str) -> str:
    name = name.strip()
    if not name:
        raise PresetEditError("A preset needs a name.")
    return name


async def list_presets() -> list[PresetRow]:
    rows = await select_rows(f"SELECT {_COLUMNS} FROM presets {_PRESET_ORDER}", (DEFAULT_LAYOUT_ID,))  # nosec B608
    return [_decoded(row) for row in rows]


async def get_preset(preset_id: str) -> PresetRow | None:
    rows = await select_rows(f"SELECT {_COLUMNS} FROM presets WHERE id = ?", (preset_id,))  # nosec B608
    return _decoded(rows[0]) if rows else None


async def get_active_preset_id() -> str:
    """The active preset; the first one when the setting names none that exists."""
    rows = await select_rows("SELECT p.id FROM settings s JOIN presets p ON p.id = s.active_preset_id WHERE s.id = 1")
    if not rows:
        rows = await select_rows(f"SELECT id FROM presets {_PRESET_ORDER} LIMIT 1", (DEFAULT_LAYOUT_ID,))  # nosec B608
    return str(rows[0]["id"]) if rows else DEFAULT_LAYOUT_ID


async def get_active_layout_blocks() -> tuple[LayoutBlock, ...]:
    """The active preset's blocks in order; the built-in order when no layout rows exist."""
    rows = await get_prompt_blocks(await get_active_preset_id())
    return tuple(layout_block(row) for row in rows) if rows else default_layout(main_prompt=SEED_MAIN_PROMPT)


async def set_active_preset_id(preset_id: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE settings SET active_preset_id = ? WHERE id = 1", (preset_id,))
        await db.commit()


async def save_live_state(preset_id: str) -> None:
    """Capture the live state into *preset_id*, reading and writing in one transaction so no endpoint or model config it names
    is deleted in between."""
    sets = ", ".join(f"{column} = ?" for column in ("config", *PRESET_LANES))
    async with immediate_tx() as db:
        state = await read_live_state(db)
        await db.execute(f"UPDATE presets SET {sets} WHERE id = ?", (*_state_values(state), preset_id))  # nosec B608


async def _endpoint_config(db: aiosqlite.Connection, endpoint_id: int | None, column: str) -> int | None:
    if endpoint_id is None:
        return None
    rows = list(await db.execute_fetchall(f"SELECT {column} FROM endpoints WHERE id = ?", (endpoint_id,)))  # nosec B608
    return rows[0][0] if rows else None


async def read_live_state(db: aiosqlite.Connection) -> PresetState:
    """The live generation state over an open connection: the config (empty without a settings row) and the lanes the live
    rows select."""
    columns = PRESET_SETTINGS + _LIVE_LANES
    rows = list(await db.execute_fetchall(f"SELECT {', '.join(columns)} FROM settings WHERE id = 1"))  # nosec B608
    if not rows:
        return {
            "config": {},
            "writer_endpoint_id": None,
            "writer_model_config_id": None,
            "agent_endpoint_id": None,
            "agent_model_config_id": None,
            "judge_endpoint_id": None,
        }
    row = rows[0]
    writer_id, agent_id = row["active_endpoint_id"], row["agent_endpoint_id"]
    lanes: PresetLanes = {
        "writer_endpoint_id": writer_id,
        "writer_model_config_id": await _endpoint_config(db, writer_id, "active_model_config_id"),
        "agent_endpoint_id": agent_id,
        "agent_model_config_id": await _endpoint_config(db, agent_id, "agent_active_model_config_id"),
        "judge_endpoint_id": row["decision_endpoint_id"],
    }
    config: PresetConfig = {
        "settings": {key: json.loads(row[key] or "{}") if key in _JSON_SETTINGS else row[key] for key in PRESET_SETTINGS},
        "models": {"agent_same_as_writer": row["agent_same_as_writer"], "judge_model": row["decision_model"]},
        "fragments": {
            "mood": {r["id"]: r["enabled"] for r in await db.execute_fetchall("SELECT id, enabled FROM mood_fragments")},
            "interactive": {
                r["id"]: r["enabled"] for r in await db.execute_fetchall("SELECT id, enabled FROM interactive_fragments")
            },
        },
    }
    return {"config": config, **lanes}


async def _insert_preset(db: aiosqlite.Connection, preset_id: str, name: str, state: PresetState) -> None:
    await db.execute(
        f"INSERT INTO presets ({_COLUMNS}) VALUES ({', '.join('?' * (3 + len(PRESET_LANES)))})",  # nosec B608
        (preset_id, name, *_state_values(state)),
    )


async def seed_default_preset(db: aiosqlite.Connection, name: str) -> None:
    """The 'default' preset over an open connection: the built-in layout, and the state the seeded rows hold."""
    await _insert_preset(db, DEFAULT_LAYOUT_ID, name, await read_live_state(db))
    await insert_layout_blocks(db, DEFAULT_LAYOUT_ID, default_layout(main_prompt=SEED_MAIN_PROMPT))
    await db.execute("UPDATE settings SET active_preset_id = ? WHERE id = 1", (DEFAULT_LAYOUT_ID,))


async def create_preset(name: str, copy_from: str | None = None) -> PresetRow | None:
    """A new preset holding the live state, with the built-in layout or a copy of *copy_from*'s blocks; None when *copy_from* is
    missing."""
    name = _name(name)
    preset_id = uuid.uuid4().hex
    if copy_from is not None:
        source = await get_prompt_blocks(copy_from)
        if not source:
            return None
        blocks = [
            replace(
                layout_block(row),
                id=uuid.uuid4().hex if row["kind"] == CUSTOM_KIND else marker_block_id(preset_id, row["kind"]),
            )
            for row in source
        ]
    else:
        blocks = list(default_layout(preset_id, SEED_MAIN_PROMPT))
    async with immediate_tx() as db:
        await _insert_preset(db, preset_id, name, await read_live_state(db))
        await insert_layout_blocks(db, preset_id, blocks)
    return await get_preset(preset_id)


async def rename_preset(preset_id: str, name: str) -> PresetRow | None:
    name = _name(name)
    async with get_db() as db:
        await db.execute("UPDATE presets SET name = ? WHERE id = ?", (name, preset_id))
        await db.commit()
    return await get_preset(preset_id)


async def delete_preset(preset_id: str) -> None:
    """Delete the preset and its blocks; the caller moves the active preset off it first."""
    async with get_db() as db:
        await db.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        await db.commit()


async def set_fragment_switches(mood: Mapping[str, int], interactive: Mapping[str, int]) -> None:
    """Switch the named fragments on or off; an id that no longer exists changes nothing."""
    async with get_db() as db:
        for table, switches in (("mood_fragments", mood), ("interactive_fragments", interactive)):
            await db.executemany(
                f"UPDATE {table} SET enabled = ? WHERE id = ?",  # nosec B608 -- fixed table names
                [(int(bool(on)), fid) for fid, on in switches.items()],
            )
        await db.commit()
