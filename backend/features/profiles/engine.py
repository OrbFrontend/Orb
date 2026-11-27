"""Switch between profiles.

The settings and endpoint rows stay the live working copy of a profile's setup, so no reader or writer of them knows profiles
exist. Leaving a profile captures the live state into it (that is the auto-save); entering one applies its state through the
existing writers. Fragments, the phrase bank and the global World marks need no capture: they are the profile's own rows, read
straight from the active profile.

A profile's lane picks are foreign-key columns, NULL once their endpoint or model config is deleted. A NULL Writer or Agent
lane keeps its live value; a NULL Judge lane turns the Judge off, as clearing it or deleting the live Judge endpoint does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ...database import (
    PRESET_SETTINGS,
    ProfileEditError,
    get_active_profile_id,
    get_endpoint,
    get_model_configs,
    get_profile,
    list_profiles,
    save_live_state,
    seed_missing_layouts,
    set_active_profile_id,
    update_decision_config,
    update_endpoint,
    update_settings,
)
from ...database import delete_profile as remove_profile
from ...database.models import PresetState, ProfileRow

# Serializes switches, and the captures and deletes that touch the active profile, so two never interleave capture and apply.
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


async def _apply_judge(endpoint_id: int | None, model: str | None) -> None:
    """Select the profile's Judge; off when it had none or its endpoint is gone."""
    endpoint = None if endpoint_id is None else await get_endpoint(endpoint_id)
    update: dict[str, Any] = {"decision_endpoint_id": endpoint_id if endpoint and endpoint["kind"] == "judge" else None}
    if model is not None:
        update["decision_model"] = model
    await update_decision_config(update)


async def apply_state(state: PresetState) -> None:
    """Make *state* live through the settings, endpoint and Judge writers."""
    config = state["config"]
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
    await _apply_judge(state["judge_endpoint_id"], models.get("judge_model"))
    if update:
        await update_settings(update)


async def _enter(profile: ProfileRow) -> None:
    await apply_state(profile)
    await set_active_profile_id(profile["id"])


async def switch_profile(profile_id: str) -> bool:
    """Leave the active profile, saving the live state into it, and enter *profile_id*. False when it does not exist."""
    async with _lock:
        target = await get_profile(profile_id)
        if target is None:
            return False
        active = await get_active_profile_id()
        if active != profile_id:
            await save_live_state(active)
            await _enter(target)
    return True


async def delete_profile(profile_id: str) -> bool:
    """Delete the profile and its library; deleting the active one enters the first profile left. False when it does not
    exist."""
    async with _lock:
        profiles = await list_profiles()
        remaining = [row for row in profiles if row["id"] != profile_id]
        if len(remaining) == len(profiles):
            return False
        if not remaining:
            raise ProfileEditError("The last profile cannot be deleted.")
        if profile_id == await get_active_profile_id():
            await _enter(remaining[0])
        await remove_profile(profile_id)
    return True


async def capture_active() -> str:
    """Save the live state into the active profile, so what it holds is what is live; returns its id."""
    async with _lock:
        active = await get_active_profile_id()
        await save_live_state(active)
    return active


async def reenter(profile_id: str) -> None:
    """Enter *profile_id* again, or the first profile when it is gone, after something rewrote the live rows or the profile
    under it; a profile that came without a layout gets the built-in one."""
    async with _lock:
        await seed_missing_layouts()
        profile = await get_profile(profile_id) or await get_profile(await get_active_profile_id())
        if profile is not None:
            await _enter(profile)
