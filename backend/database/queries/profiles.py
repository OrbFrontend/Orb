"""Profiles: a named generation config, the profile's prompt layout blocks, and its own library rows.

The settings and endpoint rows are the live working copy of the config, so no reader or writer of them knows profiles exist; a
profile's state (``config``, its lane columns and its persona) is what they held when the profile was last left. Leaving a
profile saves the live state into it (that is the auto-save); entering one writes its state into the live rows. Both happen in
one transaction with the active-profile change, so a switch lands whole or not at all. Fragments, the phrase bank with its
suggestion dismissals, the global World marks and the prompt blocks need neither: they are rows of the profile's own
(``profile_id``), read straight from the active profile.

A profile's lane picks are foreign-key columns, NULL once their endpoint or model config is deleted, or when a backup came
without the endpoints. ``config.lanes`` names each pick by URL and model name too, so a NULL lane first goes to the live
endpoint and model config those name. A lane that names none keeps its live value for the Writer and the Agent, and turns the
Judge off, as clearing it or deleting the live Judge endpoint does. The persona works the same way: a NULL ``persona_id`` goes
to the live persona ``config.persona`` names, keeps the live one when none has that name, and is cleared only when the profile
had no persona.

A workflow declares which top-level keys of its config slot are a profile's (the creative ones; credentials and the
connections to this machine's servers stay shared), and whether its on/off flag is. Entering a profile writes those keys into
the live slot, leaving the slot's other keys as they are. The runtime effects of the change (a local model to warm or free) are
the caller's: it runs them after, as after any other settings write. ``features.profiles`` serializes the changes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Mapping
from typing import Any

import aiosqlite

from ...core import DECISION_COLUMNS, STATE_COLUMNS, workflow_config_lock
from ...core.domain_types import EndpointKind
from ...core.prompt_layout import DEFAULT_LAYOUT_ID, LayoutBlock, default_layout
from ..connection import build_set_clause, get_db, immediate_tx, select_rows
from ..models import LaneRef, PersonaRef, ProfileConfig, ProfileLanes, ProfileRow, ProfileState
from ..seeds import SEED_INTERACTIVE_FRAGMENTS, SEED_MAIN_PROMPT, SEED_MOOD_FRAGMENTS, SEED_PHRASE_BANK
from .active_profile import PROFILE_ORDER, active_profile_id, get_active_profile_id
from .prompt_layouts import get_prompt_blocks, insert_layout_blocks, layout_block

# The settings columns a profile carries, and those of them stored as JSON.
PROFILE_SETTINGS: tuple[str, ...] = (
    "enable_agent",
    "enabled_tools",
    "length_guard_enabled",
    "length_guard_enforce",
    "length_guard_max_words",
    "length_guard_max_paragraphs",
    "agentic_lorebook_enabled",
    "director_individual_fragments",
    "prevent_prompt_overrides",
    "hide_streaming_until_baked",
    "reasoning_enabled_passes",
    "reasoning_prefill_passes",
    "editor_audit_toggles",
    "document_audit_enabled",
    "document_audit_autopatch",
    "document_audit_toggles",
    "document_prose_rewriter_enabled",
    "assistant_instructions",
    "assistant_reasoning",
    "assistant_tool_groups",
)
_JSON_SETTINGS = frozenset(
    {
        "enabled_tools",
        "reasoning_enabled_passes",
        "reasoning_prefill_passes",
        "editor_audit_toggles",
        "document_audit_toggles",
        "assistant_tool_groups",
    }
)
_LIVE_LANES = ("active_endpoint_id", "agent_same_as_writer", "agent_endpoint_id", "decision_endpoint_id", "decision_model")
_LIVE_OTHER = ("active_persona_id", "workflow_config", "workflow_enabled")
# The foreign-key columns holding a profile's lane picks, in ProfileLanes order.
PROFILE_LANES: tuple[str, ...] = (
    "writer_endpoint_id",
    "writer_model_config_id",
    "agent_endpoint_id",
    "agent_model_config_id",
    "judge_endpoint_id",
)
# Every foreign-key column of a profile's state: the lanes, then its persona.
_LINKS = (*PROFILE_LANES, "persona_id")
_COLUMNS = ", ".join(("id", "name", "config", *_LINKS))
# The SET clause writing a ProfileState, bound by _state_values.
_STATE_SETS = ", ".join(f"{column} = ?" for column in ("config", *_LINKS))
# The tables holding a profile's own rows, copied with it. A copy keeps each row's id but phrase_bank's surrogate one.
_LIBRARY_TABLES = (
    "prompt_blocks",
    "mood_fragments",
    "interactive_fragments",
    "phrase_bank",
    "slop_dismissals",
    "profile_worlds",
)
# By workflow id, the top-level keys of its config slot that belong to the profile, and the workflows whose on/off flag does.
# The workflow registry fills both as each workflow registers, since the workflows sit above this layer.
_WORKFLOW_DEFAULTS: dict[str, dict[str, Any]] = {}
_WORKFLOW_ENABLEMENT: set[str] = set()


def register_profile_workflow_keys(
    workflow_id: str, keys: Collection[str], defaults: Mapping[str, Any], *, enablement: bool = False
) -> None:
    """Declare which top-level keys of *workflow_id*'s config slot each profile saves and applies, and whether its on/off flag
    is the profile's too; no keys and no enablement drops the workflow."""
    if keys:
        _WORKFLOW_DEFAULTS[workflow_id] = {key: defaults[key] for key in keys}
    else:
        _WORKFLOW_DEFAULTS.pop(workflow_id, None)
    if enablement:
        _WORKFLOW_ENABLEMENT.add(workflow_id)
    else:
        _WORKFLOW_ENABLEMENT.discard(workflow_id)


def profile_workflow_keys() -> dict[str, tuple[str, ...]]:
    """By workflow id, the config keys a profile carries."""
    return {workflow_id: tuple(defaults) for workflow_id, defaults in _WORKFLOW_DEFAULTS.items()}


def profile_enabled_workflows() -> frozenset[str]:
    """The workflows whose on/off flag a profile carries."""
    return frozenset(_WORKFLOW_ENABLEMENT)


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
        "persona_id": row["persona_id"],
    }


def _state_values(state: ProfileState) -> tuple[Any, ...]:
    """*state* as the values of ``config`` and the _LINKS columns, in that order."""
    return (json.dumps(state["config"], ensure_ascii=False), *(state[column] for column in _LINKS))


def _name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ProfileEditError("A profile needs a name.")
    return name


async def list_profiles() -> list[ProfileRow]:
    rows = await select_rows(f"SELECT {_COLUMNS} FROM profiles {PROFILE_ORDER}", (DEFAULT_LAYOUT_ID,))  # nosec B608
    return [_decoded(row) for row in rows]


async def get_profile(profile_id: str) -> ProfileRow | None:
    rows = await select_rows(f"SELECT {_COLUMNS} FROM profiles WHERE id = ?", (profile_id,))  # nosec B608
    return _decoded(rows[0]) if rows else None


async def get_active_layout_blocks() -> tuple[LayoutBlock, ...]:
    """The active profile's blocks in order; the built-in order when no layout rows exist."""
    rows = await get_prompt_blocks(await get_active_profile_id())
    return tuple(layout_block(row) for row in rows) if rows else default_layout(main_prompt=SEED_MAIN_PROMPT)


async def save_live_state(profile_id: str) -> None:
    """Capture the live state into *profile_id*, reading and writing in one transaction so no endpoint or model config it names
    is deleted in between."""
    async with immediate_tx() as db:
        await _save_live_state(db, profile_id)


async def _save_live_state(db: aiosqlite.Connection, profile_id: str) -> None:
    state = await read_live_state(db)
    await db.execute(f"UPDATE profiles SET {_STATE_SETS} WHERE id = ?", (*_state_values(state), profile_id))  # nosec B608


async def capture_uploaded_profile(path: str) -> None:
    """Capture the live setup of a private, upgraded raw database before it enters the backup library."""
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await _save_live_state(db, await active_profile_id(db))
        await db.commit()


async def enter_profile(profile_id: str, *, save_leaving: bool) -> bool:
    """Make *profile_id* the active profile with its state live, in one transaction. With *save_leaving*, the live state is first
    saved into the profile being left, and entering the active profile changes nothing. False, changing nothing, when
    *profile_id* does not exist.

    Holds the workflow config lock, since entering rewrites workflow config slots that workflow code updates read-modify-write.
    """
    async with workflow_config_lock(), immediate_tx() as db:
        rows = list(await db.execute_fetchall(f"SELECT {_COLUMNS} FROM profiles WHERE id = ?", (profile_id,)))  # nosec B608
        if not rows:
            return False
        if save_leaving:
            active = await active_profile_id(db)
            if active == profile_id:
                return True
            await _save_live_state(db, active)
        await _apply_state(db, _decoded(rows[0]))
        await db.execute("UPDATE settings SET active_profile_id = ? WHERE id = 1", (profile_id,))
    return True


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


async def read_live_state(db: aiosqlite.Connection) -> ProfileState:
    """The live generation state over an open connection: the config (empty without a settings row) and the lanes the live
    rows select."""
    columns = PROFILE_SETTINGS + _LIVE_LANES + _LIVE_OTHER
    rows = list(await db.execute_fetchall(f"SELECT {', '.join(columns)} FROM settings WHERE id = 1"))  # nosec B608
    if not rows:
        return {
            "config": {},
            "writer_endpoint_id": None,
            "writer_model_config_id": None,
            "agent_endpoint_id": None,
            "agent_model_config_id": None,
            "judge_endpoint_id": None,
            "persona_id": None,
        }
    row = rows[0]
    writer_id, agent_id = row["active_endpoint_id"], row["agent_endpoint_id"]
    lanes: ProfileLanes = {
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
    persona = await _persona_ref(db, row["active_persona_id"])
    config: ProfileConfig = {
        "settings": {key: json.loads(row[key] or "{}") if key in _JSON_SETTINGS else row[key] for key in PROFILE_SETTINGS},
        "models": {"agent_same_as_writer": row["agent_same_as_writer"], "judge_model": row["decision_model"]},
        "lanes": {lane: ref for lane, ref in refs.items() if ref is not None},
        "persona": persona,
        "workflows": _profile_workflow_config(row["workflow_config"]),
        "workflow_enabled": _profile_workflow_enabled(row["workflow_enabled"]),
    }
    return {"config": config, **lanes, "persona_id": persona["persona_id"] if persona else None}


async def _persona_ref(db: aiosqlite.Connection, persona_id: int | None) -> PersonaRef | None:
    if persona_id is None:
        return None
    rows = list(await db.execute_fetchall("SELECT name FROM user_personas WHERE id = ?", (persona_id,)))
    return {"persona_id": persona_id, "name": rows[0][0]} if rows else None


def _profile_workflow_config(raw: str | None) -> dict[str, dict[str, Any]]:
    """Capture explicit per-profile workflow settings. An absent key uses the workflow's default when restored."""
    try:
        slots = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(slots, dict):
        return {}
    picked: dict[str, dict[str, Any]] = {}
    for workflow_id, keys in _WORKFLOW_DEFAULTS.items():
        slot = slots.get(workflow_id)
        if isinstance(slot, dict) and (values := {key: slot[key] for key in keys if key in slot}):
            picked[workflow_id] = values
    return picked


def _profile_workflow_enabled(raw: str | None) -> dict[str, bool]:
    """The live on/off flag of each workflow whose flag is the profile's; a missing flag reads as on, as everywhere."""
    try:
        flags = json.loads(raw or "{}")
    except ValueError:
        flags = {}
    if not isinstance(flags, dict):
        flags = {}
    return {workflow_id: bool(flags.get(workflow_id, True)) for workflow_id in sorted(_WORKFLOW_ENABLEMENT)}


async def _ids(db: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> list[int]:
    return [int(row[0]) for row in await db.execute_fetchall(sql, params)]


async def _endpoint_kind(db: aiosqlite.Connection, endpoint_id: int) -> str | None:
    rows = list(await db.execute_fetchall("SELECT kind FROM endpoints WHERE id = ?", (endpoint_id,)))
    return str(rows[0][0]) if rows else None


def _pick(matches: list[int], preferred: int | None) -> int | None:
    return preferred if preferred in matches else next(iter(matches), None)


async def _resolve_lane(
    db: aiosqlite.Connection,
    endpoint_id: int | None,
    config_id: int | None,
    ref: LaneRef | None,
    kind: EndpointKind,
    role: str | None,
) -> tuple[int | None, int | None]:
    """The lane's endpoint and model config ids, a NULL one filled from *ref*: the live *kind* endpoint with its URL (its own
    id first), and that endpoint's *role* model config with its model name (its own id first)."""
    ref = ref or {}
    url, model_name = ref.get("url"), ref.get("model_name")
    if endpoint_id is None and url:
        matches = await _ids(db, "SELECT id FROM endpoints WHERE kind = ? AND url = ? ORDER BY id", (kind, url))
        endpoint_id = _pick(matches, ref.get("endpoint_id"))
    if endpoint_id is not None and config_id is None and role is not None and model_name:
        matches = await _ids(
            db,
            "SELECT id FROM model_configs WHERE endpoint_id = ? AND role = ? AND model_name = ? ORDER BY id",
            (endpoint_id, role, model_name),
        )
        config_id = _pick(matches, ref.get("model_config_id"))
    return endpoint_id, config_id


async def _chat_lane_update(
    db: aiosqlite.Connection, endpoint_id: int | None, config_id: int | None, role: str, setting: str, column: str
) -> dict[str, int]:
    """Select the lane's model config on its endpoint, and return the settings change that selects the endpoint; {} when the
    lane is NULL or no longer fits (a chat endpoint, and a *role* model config of it)."""
    if endpoint_id is None or await _endpoint_kind(db, endpoint_id) != "chat":
        return {}
    if config_id is not None:
        sql = "SELECT id FROM model_configs WHERE id = ? AND endpoint_id = ? AND role = ?"
        if not await _ids(db, sql, (config_id, endpoint_id, role)):
            return {}
        await db.execute(f"UPDATE endpoints SET {column} = ? WHERE id = ?", (config_id, endpoint_id))  # nosec B608 -- fixed names
    return {setting: endpoint_id}


async def _judge_update(db: aiosqlite.Connection, endpoint_id: int | None, model: str | None) -> dict[str, Any]:
    """The settings change selecting the profile's Judge; off when it had none or its endpoint is gone."""
    judge = endpoint_id is not None and await _endpoint_kind(db, endpoint_id) == "judge"
    update: dict[str, Any] = {"decision_endpoint_id": endpoint_id if judge else None}
    if model is not None:
        update["decision_model"] = model
    return update


async def _persona_update(db: aiosqlite.Connection, persona_id: int | None, config: ProfileConfig) -> dict[str, int | None]:
    """The settings change selecting the profile's persona; {} when the profile never saved one, or names one that is gone."""
    if "persona" not in config:
        return {}
    ref: PersonaRef | None = config["persona"]
    if persona_id is not None and await _ids(db, "SELECT id FROM user_personas WHERE id = ?", (persona_id,)):
        return {"active_persona_id": persona_id}
    if ref is None:
        return {"active_persona_id": None}
    matches = await _ids(db, "SELECT id FROM user_personas WHERE name = ? ORDER BY id", (ref["name"],))
    found = _pick(matches, ref["persona_id"])
    return {} if found is None else {"active_persona_id": found}


async def _apply_workflows(
    db: aiosqlite.Connection, saved: Mapping[str, Mapping[str, Any]], enabled: Mapping[str, bool]
) -> None:
    """Write each workflow's saved keys into its live config slot, and its saved on/off flag, only what it declares a
    profile's today."""
    for workflow_id in sorted(_WORKFLOW_ENABLEMENT):
        if workflow_id in enabled:
            await db.execute(
                "UPDATE settings SET workflow_enabled = json_set(COALESCE(workflow_enabled, '{}'), '$.' || ?, json(?)) "
                "WHERE id = 1",
                (workflow_id, json.dumps(bool(enabled[workflow_id]))),
            )
    for workflow_id, defaults in _WORKFLOW_DEFAULTS.items():
        overrides = saved.get(workflow_id, {})
        rows = list(
            await db.execute_fetchall(
                "SELECT json_extract(workflow_config, '$.' || ?) FROM settings WHERE id = 1", (workflow_id,)
            )
        )
        raw = rows[0][0] if rows else None
        if not raw and not overrides:
            continue  # An empty live slot already reads as the workflow's complete defaults.
        slot = json.loads(raw) if isinstance(raw, str) else {}
        values = {key: overrides.get(key, default) for key, default in defaults.items()}
        await db.execute(
            "UPDATE settings SET workflow_config = json_set(COALESCE(workflow_config, '{}'), '$.' || ?, json(?)) WHERE id = 1",
            (workflow_id, json.dumps({**(slot if isinstance(slot, dict) else {}), **values}, ensure_ascii=False)),
        )


async def _apply_state(db: aiosqlite.Connection, state: ProfileState) -> None:
    """Write *state* into the live settings, endpoint and workflow rows over *db*."""
    config = state["config"]
    settings: Mapping[str, Any] = config.get("settings", {})
    update: dict[str, Any] = {key: settings[key] for key in PROFILE_SETTINGS if key in settings}
    models = config.get("models", {})
    if "agent_same_as_writer" in models:
        update["agent_same_as_writer"] = models["agent_same_as_writer"]
    refs = config.get("lanes", {})
    writer = await _resolve_lane(
        db, state["writer_endpoint_id"], state["writer_model_config_id"], refs.get("writer"), "chat", "writer"
    )
    agent = await _resolve_lane(
        db, state["agent_endpoint_id"], state["agent_model_config_id"], refs.get("agent"), "chat", "agent"
    )
    judge, _ = await _resolve_lane(db, state["judge_endpoint_id"], None, refs.get("judge"), "judge", None)
    update |= await _chat_lane_update(db, *writer, "writer", "active_endpoint_id", "active_model_config_id")
    update |= await _chat_lane_update(db, *agent, "agent", "agent_endpoint_id", "agent_active_model_config_id")
    update |= await _judge_update(db, judge, models.get("judge_model"))
    update |= await _persona_update(db, state["persona_id"], config)
    sets, values = build_set_clause(list(update), update, json_fields=_JSON_SETTINGS)
    if sets:
        await db.execute(f"UPDATE settings SET {', '.join(sets)} WHERE id = 1", values)  # nosec B608 -- names from fixed sets
    await _apply_workflows(db, config.get("workflows", {}), config.get("workflow_enabled", {}))


async def _insert_profile(db: aiosqlite.Connection, profile_id: str, name: str, state: ProfileState) -> None:
    await db.execute(
        f"INSERT INTO profiles ({_COLUMNS}) VALUES ({', '.join('?' * (3 + len(_LINKS)))})",  # nosec B608
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
    for table in _LIBRARY_TABLES + ("slop_suggestions", "slop_mining_state"):
        await db.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))  # nosec B608 -- fixed names
    await db.execute(f"UPDATE profiles SET {_STATE_SETS} WHERE id = ?", (*_state_values(await read_live_state(db)), profile_id))  # nosec B608
    await insert_layout_blocks(db, profile_id, default_layout(SEED_MAIN_PROMPT))
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
    async with immediate_tx() as db:
        state: ProfileState = await read_live_state(db)
        if copy_from is not None:
            rows = list(await db.execute_fetchall(f"SELECT {_COLUMNS} FROM profiles WHERE id = ?", (copy_from,)))  # nosec B608
            if not rows:
                return None
            if copy_from != await active_profile_id(db):
                state = _decoded(rows[0])
        await _insert_profile(db, profile_id, name, state)
        if copy_from is None:
            await insert_layout_blocks(db, profile_id, default_layout(SEED_MAIN_PROMPT))
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
            "SELECT id FROM profiles WHERE id NOT IN (SELECT DISTINCT profile_id FROM prompt_blocks)"
        )
        for (profile_id,) in list(rows):
            await insert_layout_blocks(db, profile_id, default_layout(SEED_MAIN_PROMPT))
