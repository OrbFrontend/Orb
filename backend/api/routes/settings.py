"""Global settings singleton routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import get_endpoint, get_settings, reset_to_defaults, set_enabled_tool, update_settings
from ...inference.claude_code import ENDPOINT as CLAUDE_CODE_ENDPOINT
from ...prompting.tool_catalog import has_tool
from ...workflows import gpu_swap_host, prose_rewriter_host
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
    # enabled_tools holds only model-callable tools. Drop any key that is not a
    # registered tool so non-tool feature flags can never be persisted into it.
    # Each named flag is set on its own, so a client naming one tool keeps a concurrent change to another, such as the
    # assistant's. Presets, reset and import replace the whole map through their own writers.
    tools = payload.pop("enabled_tools", None)
    for tool, on in (tools or {}).items():
        if has_tool(tool):
            await set_enabled_tool(tool, on)
    updated = await update_settings(payload)
    if "workflows_globally_enabled" in payload:
        # The GPU swap plan is off while workflows are; a render right after switching them on must not read a cached "off".
        gpu_swap_host.invalidate()
    rewriter_switches = [
        bool(payload[k]) for k in ("workflows_globally_enabled", "document_prose_rewriter_enabled") if k in payload
    ]
    if rewriter_switches:
        # Either one switches the rewriter model; it stays loaded while the other mode still uses it.
        await prose_rewriter_host.on_enabled(any(rewriter_switches))
    return public_settings(updated)


@router.post("/api/reset")
async def api_reset(data: ResetConfirm):
    """Reset mood_fragments, interactive_fragments, phrase_bank, and settings to defaults."""
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    await reset_to_defaults()
    gpu_swap_host.invalidate()
    return {"ok": True}
