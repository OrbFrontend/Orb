"""Thin HTTP facade for external ComfyUI image-generation operations."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from ...workflows import get_workflow_config
from ...workflows.image_gen.config import STYLE_DEFAULTS, normalize_config
from ...workflows.image_gen.engine import (
    CAPABILITIES,
    ImageGenerationError,
    has_graph,
    list_models,
    node_roles,
    validate_connection,
)

router = APIRouter()

MAX_INSPECTED_CLASS_TYPES = 200


async def _config_from_body(body: dict | None = None) -> dict:
    if isinstance(body, dict) and isinstance(body.get("config"), dict):
        return normalize_config(body["config"])
    return normalize_config(await get_workflow_config("image_gen"))


def _configuration_readiness(config: dict) -> dict:
    """What the saved configuration alone can tell us, with no network I/O.

    Deliberately not a health probe: the tools-panel card renders on every panel
    open, and making that wait on a remote server would trade a fast honest
    answer for a slow one. Reachability is `POST /connections/test`, which the
    Visualize modal already runs at the moment it matters.
    """
    external = config["external_comfy"]
    unresolved = sorted(
        {style["workflow"] for style in external["styles"] if style["workflow"] and not has_graph(config, style["workflow"])}
    )
    needs_checkpoint = any(
        not style["checkpoint"] and (style["workflow"] or "external_core") == "external_core" for style in external["styles"]
    )
    if unresolved:
        return {"ready": False, "reason": "unknown_workflow", "detail": f"Unknown workflow: {', '.join(unresolved)}"}
    if needs_checkpoint:
        return {
            "ready": False,
            "reason": "no_checkpoint",
            "detail": "Choose a checkpoint in settings before generating",
        }
    return {"ready": True, "reason": "", "detail": f"External ComfyUI at {external['api_url']}"}


@router.get("/api/workflows/image_gen/status")
async def api_image_gen_status():
    config = await _config_from_body()
    external = config["external_comfy"]
    return {
        "source": "external_comfy",
        "capabilities": dict(CAPABILITIES),
        "configured": any(s["checkpoint"] or s["workflow"] not in ("", "external_core") for s in external["styles"]),
        "api_url": external["api_url"],
        "default_style": config["default_style"],
        "style_count": len(external["styles"]),
        "user_graph_count": len(external["user_graphs"]),
        **_configuration_readiness(config),
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
    # An explicit test carries an unsaved config from the Advanced form; the
    # readiness probe behind the Visualize modal sends none. Only the latter may
    # answer from the cached node catalogue -- pressing Test means "look again".
    explicit = isinstance(body, dict) and isinstance(body.get("config"), dict)
    config = await _config_from_body(body)
    try:
        return await validate_connection(config, allow_cached=not explicit)
    except (ImageGenerationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.get("/api/workflows/image_gen/external/models")
async def api_image_gen_external_models():
    config = await _config_from_body()
    try:
        return {"models": await list_models(config)}
    except ImageGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None


@router.post("/api/workflows/image_gen/external/node-types")
async def api_image_gen_node_types(body: dict = Body(default={})):  # noqa: B008
    """Slot-role typing for the node classes in a graph the user is importing.

    Takes class-type names rather than the graph itself: the browser already
    parsed the graph, and this only needs to know what its node classes can do.
    """
    raw = body.get("class_types") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        raise HTTPException(status_code=422, detail="class_types (list of strings) required")
    class_types = [item for item in raw if isinstance(item, str) and item][:MAX_INSPECTED_CLASS_TYPES]
    config = await _config_from_body(body)
    try:
        return {"nodes": await node_roles(config, class_types)}
    except ImageGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
