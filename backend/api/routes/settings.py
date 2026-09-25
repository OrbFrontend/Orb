"""Global settings singleton routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import get_endpoint, get_settings, reset_to_defaults, update_settings
from ...inference.claude_code import ENDPOINT as CLAUDE_CODE_ENDPOINT
from ...prompting.tool_catalog import has_tool
from ..schemas import ResetConfirm, SettingsUpdate
from .endpoints import _check_claude_endpoint

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings():
    return await get_settings()


@router.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    payload = data.model_dump(exclude_unset=True)
    for key in ("endpoint_url", "agent_endpoint_url"):
        if isinstance(payload.get(key), str) and payload[key].lower().startswith("claude-code:"):
            _check_claude_endpoint(payload[key])
    for key in ("active_endpoint_id", "agent_endpoint_id"):
        if payload.get(key):
            endpoint = await get_endpoint(payload[key])
            if endpoint and endpoint["url"] == CLAUDE_CODE_ENDPOINT:
                _check_claude_endpoint(endpoint["url"], endpoint["api_key"], endpoint["kind"])
    # enabled_tools holds only model-callable tools. Drop any key that is not a
    # registered tool so non-tool feature flags can never be persisted into it.
    if isinstance(payload.get("enabled_tools"), dict):
        payload["enabled_tools"] = {k: v for k, v in payload["enabled_tools"].items() if has_tool(k)}
    return await update_settings(payload)


@router.post("/api/reset")
async def api_reset(data: ResetConfirm):
    """Reset mood_fragments, interactive_fragments, phrase_bank, and settings to defaults."""
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    await reset_to_defaults()
    return {"ok": True}
