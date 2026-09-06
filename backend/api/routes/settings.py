"""Global settings singleton routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import get_settings, reset_to_defaults, update_settings
from ...prompting.tool_catalog import has_tool
from ..schemas import ResetConfirm, SettingsUpdate

router = APIRouter()


@router.get("/api/settings")
async def api_get_settings():
    return await get_settings()


@router.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    payload = data.model_dump(exclude_unset=True)
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
