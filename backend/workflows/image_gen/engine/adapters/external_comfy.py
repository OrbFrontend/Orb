"""External ComfyUI generation adapter."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..comfy_client import ComfyClient, ProgressCallback
from ..contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)
from ..graph import (
    describe_render_params,
    patch_graph,
    resolve_graph,
    validate_graph_structure,
)

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
}


def _client(config: Mapping[str, Any]) -> ComfyClient:
    ext = config["external_comfy"]
    return ComfyClient(ext["api_url"], ext["api_key"])


async def validate_connection(config: Mapping[str, Any], *, allow_cached: bool = False) -> dict:
    """Prove this configuration can render, without submitting anything.

    `allow_cached` lets the Visualize modal's readiness probe reuse a recent
    node catalogue; an explicit Test connection leaves it False so the user sees
    the server as it is right now.
    """
    client = _client(config)
    stats = await client.system_stats()
    info = await client.object_info(allow_cached=allow_cached)
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
        # Apply the model override before validating so Test connection checks the
        # model that will actually run, not the filename the graph was imported
        # with -- an imported PNG pins a model from whatever machine exported it,
        # and a user-graph checkpoint slot points at the input that override targets.
        if "checkpoint" in slots:
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


def _typed_inputs(info: Mapping[str, Any], wanted: str) -> list[str]:
    """Input names on one node class whose declared type is `wanted`.

    `/object_info` declares an input as `[type, options]`, where `type` is a
    string for scalars and a list for combos. Only scalars are role candidates:
    a combo is a fixed menu, and a linked slot has no widget to patch.
    """
    spec = info.get("input")
    if not isinstance(spec, Mapping):
        return []
    declared: dict[str, Any] = {}
    for group in ("required", "optional"):
        values = spec.get(group)
        if isinstance(values, Mapping):
            declared.update(values)
    names = []
    for name, value in declared.items():
        kind = value[0] if isinstance(value, (list, tuple)) and value else None
        if kind == wanted:
            names.append(name)
    return names


async def node_roles(config: Mapping[str, Any], class_types: Sequence[str]) -> dict:
    """Which inputs of the named node classes can carry which slot role.

    The graph importer needs `/object_info` typing to build its slot picker, but
    that payload is tens of megabytes -- far too large to hand a browser just to
    populate four dropdowns. So the typing rule lives here, next to the
    validation that uses the same catalogue, and only the verdict crosses the
    wire. Unknown classes are simply absent from the result; the picker degrades
    to its name-based fallback for those.
    """
    info = await _client(config).object_info(allow_cached=True)
    roles: dict[str, dict] = {}
    for class_type in dict.fromkeys(class_types):
        entry = info.get(class_type)
        if not isinstance(entry, Mapping):
            continue
        roles[class_type] = {
            "output_node": bool(entry.get("output_node")),
            "text_inputs": _typed_inputs(entry, "STRING"),
            "seed_inputs": [name for name in _typed_inputs(entry, "INT") if "seed" in name.lower()],
        }
    return roles


async def generate(
    config: Mapping[str, Any],
    request: ImageRequest,
    *,
    checkpoint: str,
    graph_id: str,
    notes: tuple[str, ...] = (),
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
            **describe_render_params(patched, slots),
            "source": "external_comfy",
            "workflow_id": graph_id,
            "backend_model": checkpoint if shipped else None,
            "notes": list(notes),
        },
    )
