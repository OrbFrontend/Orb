"""Switch between global presets.

The settings, endpoints and fragment rows stay the live working copy, so no reader or writer knows presets exist. Leaving a
preset captures the live state into it (that is the auto-save); entering one applies its state through the existing writers.
A preset's lane picks are foreign-key columns, NULL once their endpoint or model config is deleted; a NULL lane, like a
fragment the config names that no longer exists, keeps its live value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, TypedDict, cast

from ...database import (
    PRESET_SETTINGS,
    PresetEditError,
    get_active_preset_id,
    get_endpoint,
    get_model_configs,
    get_preset,
    list_presets,
    save_live_state,
    set_active_preset_id,
    set_fragment_switches,
    update_decision_config,
    update_endpoint,
    update_settings,
)
from ...database import create_preset as insert_preset
from ...database import delete_preset as remove_preset
from ...database import rename_preset as rename_preset_row
from ...database.models import PresetRow, PresetState
from ...inference import RAW_ANSWER_CACHE


class PresetModels(TypedDict):
    """The model choices that are not lane picks; those are ``PresetState``'s foreign-key columns."""

    agent_same_as_writer: int
    judge_model: str


class PresetFragments(TypedDict):
    mood: dict[str, int]
    interactive: dict[str, int]


class PresetConfig(TypedDict, total=False):
    """``presets.config``. Every section is optional: a config read from an older or partial file applies what it has."""

    settings: dict[str, Any]  # the PRESET_SETTINGS columns, JSON ones decoded
    models: PresetModels
    fragments: PresetFragments


# Serializes switches, and the deletes that switch, so two never interleave capture and apply.
_lock = asyncio.Lock()


async def _chat_lane_update(
    endpoint_id: int | None, config_id: int | None, role: str, setting: str, column: str
) -> dict[str, int]:
    """Select the lane's model config on its endpoint, and return the settings change that selects the endpoint; {} when the
    lane is NULL or no longer fits (a chat endpoint, and a *role* model config of it)."""
    if endpoint_id is None:
        return {}
    endpoint = await get_endpoint(endpoint_id)
    if endpoint is None or endpoint["kind"] != "chat":
        return {}
    if config_id is not None:
        if not any(mc["id"] == config_id and mc["role"] == role for mc in await get_model_configs(endpoint_id)):
            return {}
        await update_endpoint(endpoint_id, {column: config_id})
    return {setting: endpoint_id}


async def _apply_judge(endpoint_id: int | None, model: str) -> None:
    if endpoint_id is None:
        return
    endpoint = await get_endpoint(endpoint_id)
    if endpoint is None or endpoint["kind"] != "judge":
        return
    await update_decision_config({"decision_endpoint_id": endpoint_id, "decision_model": model})
    RAW_ANSWER_CACHE.clear()


async def apply_state(state: PresetState) -> None:
    """Make *state* live through the settings, endpoint, Judge and fragment writers."""
    config = cast(PresetConfig, state["config"])
    settings: Mapping[str, Any] = config.get("settings", {})
    update = {key: settings[key] for key in PRESET_SETTINGS if key in settings}
    models = config.get("models", {})
    if "agent_same_as_writer" in models:
        update["agent_same_as_writer"] = models["agent_same_as_writer"]
    update |= await _chat_lane_update(
        state["writer_endpoint_id"], state["writer_model_config_id"], "writer", "active_endpoint_id", "active_model_config_id"
    )
    update |= await _chat_lane_update(
        state["agent_endpoint_id"], state["agent_model_config_id"], "agent", "agent_endpoint_id", "agent_active_model_config_id"
    )
    await _apply_judge(state["judge_endpoint_id"], models.get("judge_model", ""))
    if update:
        await update_settings(update)
    fragments = config.get("fragments")
    if fragments is not None:
        await set_fragment_switches(fragments.get("mood", {}), fragments.get("interactive", {}))


async def _enter(preset: PresetRow) -> None:
    await apply_state(preset)
    await set_active_preset_id(preset["id"])


async def switch_preset(preset_id: str) -> bool:
    """Leave the active preset, saving the live state into it, and enter *preset_id*. False when it does not exist."""
    async with _lock:
        target = await get_preset(preset_id)
        if target is None:
            return False
        active = await get_active_preset_id()
        if active != preset_id:
            await save_live_state(active)
            await _enter(target)
    return True


async def create_preset(name: str, copy_from: str | None = None) -> PresetRow | None:
    """A preset holding the live state: New with the built-in layout, Duplicate (*copy_from*) with a copy of that preset's
    blocks. None when *copy_from* does not exist."""
    return await insert_preset(name, copy_from)


async def rename_preset(preset_id: str, name: str) -> PresetRow | None:
    return await rename_preset_row(preset_id, name)


async def delete_preset(preset_id: str) -> bool:
    """Delete the preset; deleting the active one enters the first preset left. False when it does not exist."""
    async with _lock:
        if await get_preset(preset_id) is None:
            return False
        remaining = [row for row in await list_presets() if row["id"] != preset_id]
        if not remaining:
            raise PresetEditError("The last preset cannot be deleted.")
        if preset_id == await get_active_preset_id():
            await _enter(remaining[0])
        await remove_preset(preset_id)
    return True
