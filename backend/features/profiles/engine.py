"""Switch between profiles.

The settings and endpoint rows stay the live working copy of a profile's setup, so no reader or writer of them knows profiles
exist. Leaving a profile captures the live state into it (that is the auto-save); entering one applies its state through the
existing writers. Fragments, the phrase bank and the global World marks need no capture: they are the profile's own rows, read
straight from the active profile.

A profile's lane picks are foreign-key columns, NULL once their endpoint or model config is deleted, or when a backup came
without the endpoints. ``config.lanes`` names each pick by URL and model name too, so a NULL lane first goes to the live
endpoint and model config those name. A lane that names none keeps its live value for the Writer and the Agent, and turns the
Judge off, as clearing it or deleting the live Judge endpoint does. The persona works the same way: a NULL ``persona_id``
goes to the live persona ``config.persona`` names, keeps the live one when none has that name, and is cleared only when the
profile had no persona.

A workflow declares which top-level keys of its config slot are a profile's (the creative ones; credentials and the
connections to this machine's servers stay shared). Entering a profile writes those keys into the live slot under the config
lock, leaving the slot's other keys as they are. A workflow can declare its on/off flag a profile's too; entering writes the
flag straight to the settings row, without the runtime callbacks the workflow toggle route runs, so only a workflow whose
toggle has none declares it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ...core import workflow_config_lock
from ...core.domain_types import EndpointKind
from ...database import (
    PROFILE_SETTINGS,
    ProfileEditError,
    get_active_profile_id,
    get_endpoint,
    get_endpoints,
    get_model_configs,
    get_profile,
    get_user_persona,
    get_user_personas,
    get_workflow_config,
    list_profiles,
    profile_enabled_workflows,
    profile_workflow_keys,
    save_live_state,
    seed_missing_layouts,
    set_active_profile_id,
    set_workflow_config,
    set_workflow_enabled,
    update_decision_config,
    update_endpoint,
    update_settings,
)
from ...database import delete_profile as remove_profile
from ...database.models import LaneRef, PersonaRef, ProfileConfig, ProfileRow, ProfileState

# Serializes switches, and the captures and deletes that touch the active profile, so two never interleave capture and apply.
_lock = asyncio.Lock()


def _pick(matches: list[int], preferred: int | None) -> int | None:
    return preferred if preferred in matches else next(iter(matches), None)


async def _resolve_lane(
    endpoint_id: int | None, config_id: int | None, ref: LaneRef | None, kind: EndpointKind, role: str | None
) -> tuple[int | None, int | None]:
    """The lane's endpoint and model config ids, a NULL one filled from *ref*: the live *kind* endpoint with its URL (its own
    id first), and that endpoint's *role* model config with its model name (its own id first)."""
    ref = ref or {}
    url, model_name = ref.get("url"), ref.get("model_name")
    if endpoint_id is None and url:
        matches = [e["id"] for e in await get_endpoints(kind) if e["url"] == url]
        endpoint_id = _pick(matches, ref.get("endpoint_id"))
    if endpoint_id is not None and config_id is None and role is not None and model_name:
        configs = await get_model_configs(endpoint_id)
        matches = [mc["id"] for mc in configs if mc["role"] == role and mc["model_name"] == model_name]
        config_id = _pick(matches, ref.get("model_config_id"))
    return endpoint_id, config_id


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


async def _persona_update(persona_id: int | None, config: ProfileConfig) -> dict[str, int | None]:
    """The settings change selecting the profile's persona; {} when the profile never saved one, or names one that is gone."""
    if "persona" not in config:
        return {}
    ref: PersonaRef | None = config["persona"]
    if persona_id is not None and await get_user_persona(persona_id) is not None:
        return {"active_persona_id": persona_id}
    if ref is None:
        return {"active_persona_id": None}
    matches = [p["id"] for p in await get_user_personas() if p["name"] == ref["name"]]
    found = _pick(matches, ref["persona_id"])
    return {} if found is None else {"active_persona_id": found}


async def _apply_workflows(saved: Mapping[str, Mapping[str, Any]], enabled: Mapping[str, bool]) -> None:
    """Write each workflow's saved keys into its live config slot, and its saved on/off flag, only what it declares a
    profile's today."""
    for workflow_id in profile_enabled_workflows():
        if workflow_id in enabled:
            await set_workflow_enabled(workflow_id, enabled[workflow_id])
    for workflow_id, keys in profile_workflow_keys().items():
        values = {key: saved[workflow_id][key] for key in keys if key in saved.get(workflow_id, {})}
        if not values:
            continue
        async with workflow_config_lock():
            await set_workflow_config(workflow_id, {**await get_workflow_config(workflow_id), **values})


async def apply_state(state: ProfileState) -> None:
    """Make *state* live through the settings, endpoint and Judge writers."""
    config = state["config"]
    settings: Mapping[str, Any] = config.get("settings", {})
    update = {key: settings[key] for key in PROFILE_SETTINGS if key in settings}
    models = config.get("models", {})
    if "agent_same_as_writer" in models:
        update["agent_same_as_writer"] = models["agent_same_as_writer"]
    refs = config.get("lanes", {})
    writer = await _resolve_lane(
        state["writer_endpoint_id"], state["writer_model_config_id"], refs.get("writer"), "chat", "writer"
    )
    agent = await _resolve_lane(state["agent_endpoint_id"], state["agent_model_config_id"], refs.get("agent"), "chat", "agent")
    judge, _ = await _resolve_lane(state["judge_endpoint_id"], None, refs.get("judge"), "judge", None)
    update |= await _chat_lane_update(*writer, "writer", "active_endpoint_id", "active_model_config_id")
    update |= await _chat_lane_update(*agent, "agent", "agent_endpoint_id", "agent_active_model_config_id")
    await _apply_judge(judge, models.get("judge_model"))
    update |= await _persona_update(state["persona_id"], config)
    if update:
        await update_settings(update)
    await _apply_workflows(config.get("workflows", {}), config.get("workflow_enabled", {}))


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
