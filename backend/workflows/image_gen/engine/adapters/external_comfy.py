"""External ComfyUI generation adapter."""

from __future__ import annotations

from typing import Any, Mapping

from ..comfy_client import ComfyClient, ProgressCallback
from ..contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)
from ..graph import patch_graph, resolve_graph, validate_graph_structure

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
}


def _client(config: Mapping[str, Any]) -> ComfyClient:
    ext = config["external_comfy"]
    return ComfyClient(ext["api_url"], ext["api_key"])


async def validate_connection(config: Mapping[str, Any]) -> dict:
    client = _client(config)
    stats = await client.system_stats()
    info = await client.object_info()
    checked: set[str] = set()
    selections = [(config["external_comfy"]["workflow"], config["external_comfy"]["checkpoint"])]
    selections.extend(
        (
            s["workflow"] or config["external_comfy"]["workflow"],
            s["checkpoint"] or config["external_comfy"]["checkpoint"],
        )
        for s in config["external_comfy"]["styles"]
    )
    models: list[str] | None = None

    async def available_checkpoints() -> list[str]:
        nonlocal models
        if models is None:
            models = await client.models("checkpoints")
        return models

    for graph_id, checkpoint in selections:
        key = f"{graph_id}\0{checkpoint}"
        if key in checked:
            continue
        checked.add(key)
        graph, slots, shipped = resolve_graph(config, graph_id)
        if shipped:
            if not checkpoint:
                raise ValueError("Select a checkpoint before generating")
            if checkpoint not in await available_checkpoints():
                raise ValueError(f"checkpoint {checkpoint!r} is not present on the server")
            graph, _ = patch_graph(
                graph,
                slots,
                prompt="connection test",
                negative_prompt="",
                seed=0,
                checkpoint=checkpoint,
            )
        validate_graph_structure(graph, slots, info)
    try:
        discovered = await available_checkpoints()
    except ImageGenerationError:
        # Discovery only fills the settings dropdown. A server that validated
        # every selected graph is connected whether or not it lists models.
        discovered = []
    return {
        "ok": True,
        "capabilities": dict(CAPABILITIES),
        "system": _safe_system_summary(stats),
        "models": discovered,
    }


def _safe_system_summary(stats: Mapping[str, Any]) -> dict:
    system_value = stats.get("system")
    system: Mapping[str, Any] = system_value if isinstance(system_value, Mapping) else {}
    devices_value = stats.get("devices")
    devices: list[Any] = devices_value if isinstance(devices_value, list) else []
    safe_devices = []
    for d in devices:
        if isinstance(d, Mapping):
            safe_devices.append(
                {
                    "name": str(d.get("name", ""))[:160],
                    "vram_total": d.get("vram_total"),
                }
            )
    return {
        "comfyui_version": str(system.get("comfyui_version", ""))[:80],
        "devices": safe_devices,
    }


async def list_models(config: Mapping[str, Any]) -> list[str]:
    return await _client(config).models("checkpoints")


async def generate(
    config: Mapping[str, Any],
    request: ImageRequest,
    *,
    checkpoint: str,
    graph_id: str,
    progress: ProgressCallback | None = None,
) -> ImageResult:
    graph, slots, shipped = resolve_graph(config, graph_id)
    patched, output_node = patch_graph(
        graph,
        slots,
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        seed=request.seed,
        checkpoint=checkpoint,
    )
    result = await _client(config).generate(
        patched,
        output_node,
        timeout_seconds=request.timeout_seconds,
        progress=progress,
    )
    return ImageResult(
        image_bytes=result.image_bytes,
        mime=result.mime,
        backend_info={
            **result.backend_info,
            "source": "external_comfy",
            "workflow_id": graph_id,
            "backend_model": checkpoint if shipped else None,
        },
    )
