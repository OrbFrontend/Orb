"""Global settings singleton routes."""

from __future__ import annotations

from fastapi import APIRouter

from ...database import get_settings, update_settings
from ...inference import TOOLS
from ..schemas import SettingsUpdate

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
        payload["enabled_tools"] = {k: v for k, v in payload["enabled_tools"].items() if k in TOOLS}
    return await update_settings(payload)
