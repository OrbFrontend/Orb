"""Profiles: a named generation config, the profile's prompt layout blocks, and its own library rows.

The settings and endpoint rows are the live working copy of the config; a profile's state (``config`` and its lane columns) is
what they held when the profile was last left. Fragments, the phrase bank with its suggestion dismissals, and the global World
marks are rows of the profile's own (``profile_id``), read straight from the active profile. ``features.profiles`` owns the
switching rules.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection
from dataclasses import replace
from typing import Any

import aiosqlite

from ...core import DECISION_COLUMNS, STATE_COLUMNS
from ...core.prompt_layout import CUSTOM_KIND, DEFAULT_LAYOUT_ID, LayoutBlock, default_layout, marker_block_id
from ..connection import get_db, immediate_tx, select_rows
from ..models import LaneRef, PresetConfig, PresetLanes, PresetState, ProfileRow
from ..seeds import SEED_INTERACTIVE_FRAGMENTS, SEED_MAIN_PROMPT, SEED_MOOD_FRAGMENTS, SEED_PHRASE_BANK
from .prompt_layouts import get_prompt_blocks, insert_layout_blocks, layout_block

# The settings columns a profile carries, and those of them stored as JSON.
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
# The foreign-key columns holding a profile's lane picks, in PresetLanes order.
PRESET_LANES: tuple[str, ...] = (
    "writer_endpoint_id",
    "writer_model_config_id",
    "agent_endpoint_id",
    "agent_model_config_id",
    "judge_endpoint_id",
)
_COLUMNS = ", ".join(("id", "name", "config", *PRESET_LANES))
_PROFILE_ORDER = "ORDER BY id != ?, name COLLATE NOCASE"
# The tables holding a profile's own rows, copied with it. phrase_bank's surrogate id is renumbered in the copy.
_LIBRARY_TABLES = ("mood_fragments", "interactive_fragments", "phrase_bank", "slop_dismissals", "profile_worlds")


class ProfileEditError(ValueError):
    """A profile edit the rules refuse; the message is safe to show."""


def _decoded(row: Any) -> ProfileRow:
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
        raise ProfileEditError("A profile needs a name.")
    return name


async def list_profiles() -> list[ProfileRow]:
    rows = await select_rows(f"SELECT {_COLUMNS} FROM profiles {_PROFILE_ORDER}", (DEFAULT_LAYOUT_ID,))  # nosec B608
    return [_decoded(row) for row in rows]


async def get_profile(profile_id: str) -> ProfileRow | None:
    rows = await select_rows(f"SELECT {_COLUMNS} FROM profiles WHERE id = ?", (profile_id,))  # nosec B608
    return _decoded(rows[0]) if rows else None


async def get_active_profile_id() -> str:
    """The active profile; the first one when the setting names none that exists."""
    rows = await select_rows("SELECT p.id FROM settings s JOIN profiles p ON p.id = s.active_profile_id WHERE s.id = 1")
    if not rows:
        rows = await select_rows(f"SELECT id FROM profiles {_PROFILE_ORDER} LIMIT 1", (DEFAULT_LAYOUT_ID,))  # nosec B608
    return str(rows[0]["id"]) if rows else DEFAULT_LAYOUT_ID


async def get_active_layout_blocks() -> tuple[LayoutBlock, ...]:
    """The active profile's blocks in order; the built-in order when no layout rows exist."""
    rows = await get_prompt_blocks(await get_active_profile_id())
    return tuple(layout_block(row) for row in rows) if rows else default_layout(main_prompt=SEED_MAIN_PROMPT)


async def set_active_profile_id(profile_id: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE settings SET active_profile_id = ? WHERE id = 1", (profile_id,))
        await db.commit()


async def save_live_state(profile_id: str) -> None:
    """Capture the live state into *profile_id*, reading and writing in one transaction so no endpoint or model config it names
    is deleted in between."""
    sets = ", ".join(f"{column} = ?" for column in ("config", *PRESET_LANES))
    async with immediate_tx() as db:
        state = await read_live_state(db)
        await db.execute(f"UPDATE profiles SET {sets} WHERE id = ?", (*_state_values(state), profile_id))  # nosec B608


async def _endpoint_config(db: aiosqlite.Connection, endpoint_id: int | None, column: str) -> int | None:
    if endpoint_id is None:
        return None
    rows = list(await db.execute_fetchall(f"SELECT {column} FROM endpoints WHERE id = ?", (endpoint_id,)))  # nosec B608
    return rows[0][0] if rows else None


async def _lane_ref(db: aiosqlite.Connection, endpoint_id: int | None, config_id: int | None) -> LaneRef | None:
    """What the lane pick names, so a backup that leaves out the endpoints can still find them; None without a pick."""
    if endpoint_id is None:
        return None
    rows = list(await db.execute_fetchall("SELECT url FROM endpoints WHERE id = ?", (endpoint_id,)))
    if not rows:
        return None
    ref: LaneRef = {"endpoint_id": endpoint_id, "url": rows[0][0]}
    if config_id is not None:
        rows = list(await db.execute_fetchall("SELECT model_name FROM model_configs WHERE id = ?", (config_id,)))
        if rows:
            ref |= {"model_config_id": config_id, "model_name": rows[0][0]}
    return ref


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
    refs = {
        "writer": await _lane_ref(db, writer_id, lanes["writer_model_config_id"]),
        "agent": await _lane_ref(db, agent_id, lanes["agent_model_config_id"]),
        "judge": await _lane_ref(db, lanes["judge_endpoint_id"], None),
    }
    config: PresetConfig = {
        "settings": {key: json.loads(row[key] or "{}") if key in _JSON_SETTINGS else row[key] for key in PRESET_SETTINGS},
        "models": {"agent_same_as_writer": row["agent_same_as_writer"], "judge_model": row["decision_model"]},
        "lanes": {lane: ref for lane, ref in refs.items() if ref is not None},
    }
    return {"config": config, **lanes}


async def _insert_profile(db: aiosqlite.Connection, profile_id: str, name: str, state: PresetState) -> None:
    await db.execute(
        f"INSERT INTO profiles ({_COLUMNS}) VALUES ({', '.join('?' * (3 + len(PRESET_LANES)))})",  # nosec B608
        (profile_id, name, *_state_values(state)),
    )


# The parts of a profile's library the seeds fill: "fragments" (mood and interactive) and "phrase_bank".
SEEDED_PARTS: tuple[str, ...] = ("fragments", "phrase_bank")


def seed_library_statements(profile_id: str, parts: Collection[str] = SEEDED_PARTS) -> list[tuple[str, tuple[Any, ...]]]:
    """The inserts that give *profile_id* the seed rows of *parts*, as (sql, params) for any connection to run."""
    statements: list[tuple[str, tuple[Any, ...]]] = []
    if "fragments" in parts:
        for f in SEED_MOOD_FRAGMENTS:
            statements.append(
                (
                    "INSERT INTO mood_fragments (profile_id, id, label, description, prompt_text, negative_prompt) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (profile_id, f["id"], f["label"], f["description"], f["prompt_text"], f["negative_prompt"]),
                )
            )
        columns = ("id", "label", "description", "field_type", "required", "enabled", "injection_label", "sort_order")
        columns += DECISION_COLUMNS + STATE_COLUMNS
        sql = (
            f"INSERT INTO interactive_fragments (profile_id, {', '.join(columns)}) "  # nosec B608 -- fixed columns
            f"VALUES (?, {', '.join('?' * len(columns))})"
        )
        for seed in SEED_INTERACTIVE_FRAGMENTS:
            row = {"description": "", "enabled": True, **seed}
            values = [row.get(column) for column in columns]
            params = (profile_id, *(json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in values))
            statements.append((sql, params))
    if "phrase_bank" in parts:
        # A seed entry is either a raw regex pattern (str) or a list of literal variant phrases.
        sql = "INSERT INTO phrase_bank (profile_id, variants, kind, pattern) VALUES (?, ?, ?, ?)"
        for entry in SEED_PHRASE_BANK:
            params = (
                (profile_id, "[]", "regex", entry)
                if isinstance(entry, str)
                else (profile_id, json.dumps(entry), "literal", None)
            )
            statements.append((sql, params))
    return statements


async def seed_profile_library(db: aiosqlite.Connection, profile_id: str) -> None:
    """The seed fragments and phrase bank, as *profile_id*'s rows."""
    for sql, params in seed_library_statements(profile_id):
        await db.execute(sql, params)


async def seed_default_profile(db: aiosqlite.Connection, name: str) -> None:
    """The 'default' profile over an open connection: the built-in layout, the seed library, and the state the seeded settings
    and endpoint hold."""
    await _insert_profile(db, DEFAULT_LAYOUT_ID, name, await read_live_state(db))
    await insert_layout_blocks(db, DEFAULT_LAYOUT_ID, default_layout(main_prompt=SEED_MAIN_PROMPT))
    await seed_profile_library(db, DEFAULT_LAYOUT_ID)
    await db.execute("UPDATE settings SET active_profile_id = ? WHERE id = 1", (DEFAULT_LAYOUT_ID,))


async def reset_profile(db: aiosqlite.Connection, profile_id: str) -> None:
    """Put *profile_id*'s layout and library back to the built-in ones and its state to the live one, over an open connection;
    the caller has reset the live rows."""
    await db.execute("DELETE FROM prompt_blocks WHERE layout_id = ?", (profile_id,))
    for table in _LIBRARY_TABLES + ("slop_suggestions", "slop_mining_state"):
        await db.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))  # nosec B608 -- fixed names
    sets = ", ".join(f"{column} = ?" for column in ("config", *PRESET_LANES))
    await db.execute(f"UPDATE profiles SET {sets} WHERE id = ?", (*_state_values(await read_live_state(db)), profile_id))  # nosec B608
    await insert_layout_blocks(db, profile_id, default_layout(profile_id, SEED_MAIN_PROMPT))
    await seed_profile_library(db, profile_id)


async def _columns(db: aiosqlite.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in await db.execute_fetchall(f"PRAGMA table_info({table})")]  # nosec B608 -- fixed names


async def _copy_library(db: aiosqlite.Connection, source: str, target: str) -> None:
    for table in _LIBRARY_TABLES:
        columns = [c for c in await _columns(db, table) if c != "profile_id" and not (table == "phrase_bank" and c == "id")]
        names = ", ".join(columns)
        await db.execute(
            f"INSERT INTO {table} (profile_id, {names}) SELECT ?, {names} FROM {table} "  # nosec B608 -- schema-derived names
            "WHERE profile_id = ? ORDER BY rowid",
            (target, source),
        )


async def create_profile(name: str, copy_from: str | None = None) -> ProfileRow | None:
    """A new profile. With *copy_from*, a copy of that profile: its layout, library and global Worlds, and its state (the live
    one when it is the active profile). Without, the built-in layout and seed library, no global Worlds, and the live state.
    None when *copy_from* is missing."""
    name = _name(name)
    profile_id = uuid.uuid4().hex
    active = await get_active_profile_id()
    async with immediate_tx() as db:
        state = await read_live_state(db)
        if copy_from is None:
            blocks = list(default_layout(profile_id, SEED_MAIN_PROMPT))
        else:
            source = await get_profile(copy_from)
            if source is None:
                return None
            if copy_from != active:
                state = source
            blocks = [
                replace(
                    layout_block(row),
                    id=uuid.uuid4().hex if row["kind"] == CUSTOM_KIND else marker_block_id(profile_id, row["kind"]),
                )
                for row in await get_prompt_blocks(copy_from)
            ]
        await _insert_profile(db, profile_id, name, state)
        await insert_layout_blocks(db, profile_id, blocks)
        if copy_from is None:
            await seed_profile_library(db, profile_id)
        else:
            await _copy_library(db, copy_from, profile_id)
    return await get_profile(profile_id)


async def rename_profile(profile_id: str, name: str) -> ProfileRow | None:
    name = _name(name)
    async with get_db() as db:
        await db.execute("UPDATE profiles SET name = ? WHERE id = ?", (name, profile_id))
        await db.commit()
    return await get_profile(profile_id)


async def delete_profile(profile_id: str) -> None:
    """Delete the profile with its blocks and library; the caller moves the active profile off it first."""
    async with get_db() as db:
        await db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await db.commit()


async def seed_missing_layouts() -> None:
    """Give the built-in layout to every profile that has no blocks, as one a Backups apply brought in without its setup."""
    async with immediate_tx() as db:
        rows = await db.execute_fetchall(
            "SELECT id FROM profiles WHERE id NOT IN (SELECT DISTINCT layout_id FROM prompt_blocks)"
        )
        for (profile_id,) in list(rows):
            await insert_layout_blocks(db, profile_id, default_layout(profile_id, SEED_MAIN_PROMPT))
