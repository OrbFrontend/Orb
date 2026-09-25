"""Endpoint and model-config CRUD routes."""

from __future__ import annotations

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
from ...inference.claude_code import (
    ClaudeCodeError,
    cli_status,
    local_only_active,
    require_local_only,
)
from ..schemas import (
    EndpointCreate,
    EndpointUpdate,
    ModelConfigCreate,
    ModelConfigUpdate,
)

router = APIRouter()


def _check_claude_endpoint(url: str, api_key: str = "", kind: EndpointKind = "chat") -> None:
    if url.lower().startswith("claude-code:"):
        if url != CLAUDE_CODE_ENDPOINT:
            raise HTTPException(status_code=422, detail="Unsupported Claude Code endpoint marker")
        try:
            require_local_only()
        except ClaudeCodeError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        if api_key or kind != "chat":
            raise HTTPException(
                status_code=422, detail="Claude Code uses local CLI login and chat endpoints only; leave API Key empty"
            )


@router.get("/api/claude-code/status")
async def api_claude_code_status():
    try:
        return await cli_status()
    except ClaudeCodeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.get("/api/claude-code/availability")
async def api_claude_code_availability():
    return {"local_only": local_only_active()}


@router.get("/api/endpoints")
async def api_get_endpoints(kind: EndpointKind | None = None):
    """Saved endpoints, narrowed to one lane's pool when *kind* is given."""
    return await get_endpoints(kind)


@router.get("/api/endpoints/{endpoint_id}")
async def api_get_endpoint(endpoint_id: int):
    result = await get_endpoint(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return result


@router.post("/api/endpoints")
async def api_create_endpoint(data: EndpointCreate):
    _check_claude_endpoint(data.url, data.api_key, data.kind)
    return await create_endpoint(data.url, data.api_key, data.kind)


@router.put("/api/endpoints/{endpoint_id}")
async def api_update_endpoint(endpoint_id: int, data: EndpointUpdate):
    original = await get_endpoint(endpoint_id)
    if original is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    url = data.url if data.url is not None else original["url"]
    api_key = data.api_key if data.api_key is not None else original["api_key"]
    _check_claude_endpoint(url, api_key, original["kind"])
    if url == CLAUDE_CODE_ENDPOINT and (data.proxy if data.proxy is not None else original["proxy"]):
        raise HTTPException(status_code=422, detail="Claude Code local transport does not use an HTTP proxy")
    result = await update_endpoint(endpoint_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return result


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
        _check_claude_endpoint(endpoint["url"], endpoint["api_key"], endpoint["kind"])
        return {"models": []}

    client = LLMClient(
        endpoint["url"],
        endpoint["api_key"],
        proxy=endpoint.get("proxy"),
    )
    try:
        models = await client.list_models()
    except httpx.HTTPStatusError as exc:
        sentence = redact(provider_sentence(exc.response.text), endpoint["api_key"])
        suffix = f": {sentence}" if sentence else ""
        raise HTTPException(
            status_code=502,
            detail=f"Model discovery failed (provider HTTP {exc.response.status_code}){suffix}",
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
