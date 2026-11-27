"""Global settings singleton routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import get_endpoint, get_settings, reset_to_defaults, set_enabled_tool, update_settings
from ...inference.claude_code import ENDPOINT as CLAUDE_CODE_ENDPOINT
from ...prompting.tool_catalog import has_tool
from ...workflows import live_settings_changed
from ..schemas import ResetConfirm, SettingsUpdate
from .endpoints import check_claude_endpoint, public_settings

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings():
    return public_settings(await get_settings())


@router.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    payload = data.model_dump(exclude_unset=True)
    for key in ("active_endpoint_id", "agent_endpoint_id"):
        if payload.get(key):
            endpoint = await get_endpoint(payload[key])
            if endpoint and endpoint["url"] == CLAUDE_CODE_ENDPOINT:
                check_claude_endpoint(endpoint["url"], endpoint["api_key"], endpoint["kind"])
    before = await get_settings()
    # enabled_tools holds only model-callable tools. Drop any key that is not a
    # registered tool so non-tool feature flags can never be persisted into it.
    # Each named flag is set on its own, so a client naming one tool keeps a concurrent change to another, such as the
    # assistant's. Profiles, reset and import replace the whole map through their own writers.
    tools = payload.pop("enabled_tools", None)
    for tool, on in (tools or {}).items():
        if has_tool(tool):
            await set_enabled_tool(tool, on)
    await update_settings(payload)
    await live_settings_changed(before)
    return public_settings(await get_settings())


@router.post("/api/reset")
async def api_reset(data: ResetConfirm):
    """Reset the settings, endpoints, and the active profile's library, layout and switches to defaults."""
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    before = await get_settings()
    await reset_to_defaults()
    await live_settings_changed(before)
    return {"ok": True}
