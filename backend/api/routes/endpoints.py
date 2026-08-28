"""Endpoint and model-config CRUD routes."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

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
from ..schemas import (
    EndpointCreate,
    EndpointUpdate,
    ModelConfigCreate,
    ModelConfigUpdate,
)

router = APIRouter()


@router.get("/api/endpoints")
async def api_get_endpoints():
    return await get_endpoints()


@router.get("/api/endpoints/{endpoint_id}")
async def api_get_endpoint(endpoint_id: int):
    result = await get_endpoint(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return result


@router.post("/api/endpoints")
async def api_create_endpoint(data: EndpointCreate):
    return await create_endpoint(data.url, data.api_key)


@router.put("/api/endpoints/{endpoint_id}")
async def api_update_endpoint(endpoint_id: int, data: EndpointUpdate):
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
