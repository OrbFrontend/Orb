"""Thin HTTP facade for external ComfyUI image-generation operations."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ...workflows import get_workflow_config
from ...workflows.image_gen.config import STYLE_DEFAULTS, normalize_config
from ...workflows.image_gen.engine import (
    CAPABILITIES,
    ImageGenerationError,
    list_models,
    validate_connection,
)

router = APIRouter()


async def _config_from_body(body: dict | None = None) -> dict:
    if isinstance(body, dict) and isinstance(body.get("config"), dict):
        return normalize_config(body["config"])
    return normalize_config(await get_workflow_config("image_gen"))


@router.get("/api/workflows/image_gen/status")
async def api_image_gen_status():
    config = await _config_from_body()
    external = config["external_comfy"]
    return {
        "source": "external_comfy",
        "capabilities": dict(CAPABILITIES),
        "configured": bool(external["checkpoint"]) or external["workflow"] != "external_core",
        "api_url": external["api_url"],
        "managed_local": {
            "available": False,
            "reason": "Managed local image generation is not included in this stage",
        },
    }


@router.get("/api/workflows/image_gen/styles")
async def api_image_gen_styles():
    config = await _config_from_body()
    defaults = {s["id"]: s for s in STYLE_DEFAULTS}
    return {
        "source": "external_comfy",
        "default_style": config["default_style"],
        "styles": [
            {
                **style,
                "prompt_default": defaults.get(style["id"], {}).get("prompt", ""),
                "negative_prompt_default": defaults.get(style["id"], {}).get("negative_prompt", ""),
            }
            for style in config["external_comfy"]["styles"]
        ],
    }


@router.post("/api/workflows/image_gen/connections/test")
async def api_image_gen_test_connection(body: dict = Body(default={})):  # noqa: B008
    config = await _config_from_body(body)
    try:
        return await validate_connection(config)
    except (ImageGenerationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/api/workflows/image_gen/external/models")
async def api_image_gen_external_models():
    config = await _config_from_body()
    try:
        return {"models": await list_models(config)}
    except ImageGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
