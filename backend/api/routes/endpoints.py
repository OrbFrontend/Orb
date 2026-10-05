"""Endpoint and model-config CRUD routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException

from ...core.domain_types import EndpointKind
from ...database import (
    create_endpoint,
    create_model_config,
    delete_endpoint,
    delete_model_config,
    get_endpoint,
    get_endpoints,
    get_model_configs,
    update_endpoint,
    update_model_config,
)
from ...inference import LLMClient, provider_sentence, redact
from ...inference.claude_code import ENDPOINT as CLAUDE_CODE_ENDPOINT
from ...inference.claude_code import ClaudeCodeError, cli_status
from ..schemas import EndpointCreate, EndpointUpdate, ModelConfigCreate, ModelConfigUpdate

router = APIRouter()

# Saved keys stay on the server: endpoint and settings responses carry only a hint, and the key itself goes out one endpoint at
# a time from /api/endpoints/{id}/api-key, when the user asks to see it.
_KEY_MASK = "••••••••"


def api_key_hint(key: str) -> str:
    """What a response shows of a saved key: empty when there is none, its last four characters only on a key long enough to
    keep the rest secret."""
    if not key:
        return ""
    return _KEY_MASK + key[-4:] if len(key) >= 16 else _KEY_MASK


def public_endpoint(row: Mapping[str, Any]) -> dict[str, Any]:
    """An endpoint row as responses show it, with ``api_key_hint`` in place of ``api_key``."""
    out = dict(row)
    out["api_key_hint"] = api_key_hint(out.pop("api_key", "") or "")
    return out


def public_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Settings as responses show them, without the keys overlaid from the active endpoints; their rows carry the hints."""
    return {key: value for key, value in settings.items() if key not in ("api_key", "agent_api_key")}


def check_claude_endpoint(url: str, api_key: str = "", kind: EndpointKind = "chat") -> None:
    if url.lower().startswith("claude-code:"):
        if url != CLAUDE_CODE_ENDPOINT:
            raise HTTPException(status_code=422, detail="Unsupported Claude Code endpoint marker")
        if api_key or kind != "chat":
            raise HTTPException(
                status_code=422, detail="Claude Code uses local CLI login and chat endpoints only; leave API Key empty"
            )


@router.get("/api/claude-code/status")
async def api_claude_code_status():
    try:
        return await cli_status()
    except ClaudeCodeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.get("/api/endpoints")
async def api_get_endpoints(kind: EndpointKind | None = None):
    """Saved endpoints, narrowed to one lane's pool when *kind* is given."""
    return [public_endpoint(row) for row in await get_endpoints(kind)]


@router.get("/api/endpoints/{endpoint_id}")
async def api_get_endpoint(endpoint_id: int):
    result = await get_endpoint(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return public_endpoint(result)


@router.get("/api/endpoints/{endpoint_id}/api-key")
async def api_reveal_endpoint_key(endpoint_id: int):
    """The one response that carries a saved key, for the key field's show button."""
    result = await get_endpoint(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return {"api_key": result["api_key"]}


@router.post("/api/endpoints")
async def api_create_endpoint(data: EndpointCreate):
    check_claude_endpoint(data.url, data.api_key, data.kind)
    return public_endpoint(await create_endpoint(data.url, data.api_key, data.kind))


@router.put("/api/endpoints/{endpoint_id}")
async def api_update_endpoint(endpoint_id: int, data: EndpointUpdate):
    original = await get_endpoint(endpoint_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    url = data.url if data.url is not None else original["url"]
    api_key = data.api_key if data.api_key is not None else original["api_key"]
    check_claude_endpoint(url, api_key, original["kind"])
    if url == CLAUDE_CODE_ENDPOINT and (data.proxy if data.proxy is not None else original["proxy"]):
        raise HTTPException(status_code=422, detail="Claude Code local transport does not use an HTTP proxy")
    try:
        result = await update_endpoint(endpoint_id, data.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return public_endpoint(result)


@router.delete("/api/endpoints/{endpoint_id}")
async def api_delete_endpoint(endpoint_id: int):
    if not await delete_endpoint(endpoint_id):
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return {"ok": True}


@router.get("/api/endpoints/{endpoint_id}/models")
async def api_get_model_configs(endpoint_id: int):
    return await get_model_configs(endpoint_id)


@router.get("/api/endpoints/{endpoint_id}/available-models")
async def api_get_available_models(endpoint_id: int):
    endpoint = await get_endpoint(endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    if endpoint["url"] == CLAUDE_CODE_ENDPOINT:
        check_claude_endpoint(endpoint["url"], endpoint["api_key"], endpoint["kind"])
        return {"models": []}

    client = LLMClient(endpoint["url"], endpoint["api_key"], proxy=endpoint.get("proxy"))
    try:
        models = await client.list_models()
    except httpx.HTTPStatusError as exc:
        sentence = redact(provider_sentence(exc.response.text), endpoint["api_key"])
        suffix = f": {sentence}" if sentence else ""
        raise HTTPException(
            status_code=502, detail=f"Model discovery failed (provider HTTP {exc.response.status_code}){suffix}"
        ) from None
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Model discovery could not reach the endpoint") from None
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"Model discovery failed: {exc}") from None
    return {"models": models}


@router.post("/api/endpoints/{endpoint_id}/models")
async def api_create_model_config(endpoint_id: int, data: ModelConfigCreate):
    try:
        return await create_model_config(endpoint_id, data.model_dump())
    except Exception as e:
        if "FOREIGN KEY constraint failed" in str(e):
            raise HTTPException(status_code=404, detail="Endpoint not found") from e
        raise


@router.put("/api/models/{config_id}")
async def api_update_model_config(config_id: int, data: ModelConfigUpdate):
    result = await update_model_config(config_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="Model config not found")
    return result


@router.delete("/api/models/{config_id}")
async def api_delete_model_config(config_id: int):
    if not await delete_model_config(config_id):
        raise HTTPException(status_code=404, detail="Model config not found")
    return {"ok": True}
