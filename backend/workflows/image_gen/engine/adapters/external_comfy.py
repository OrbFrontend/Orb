"""External ComfyUI generation adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..comfy_client import ComfyClient, ProgressCallback
from ..contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
)
from ..graph import (
    describe_render_params,
    is_image_upload,
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
    # A style with no workflow assigned has no graph to validate against the
    # server; readiness reports that gap separately, so skip it here rather than
    # fail the whole probe. Only pinned workflows are checked.
    selections = [(s["workflow"], s["checkpoint"]) for s in config["external_comfy"]["styles"] if s["workflow"]]
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
        graph, slots = resolve_graph(config, graph_id)
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


def _declared_inputs(info: Mapping[str, Any]) -> dict[str, Any]:
    """Every declared input of one node class, required and optional alike."""
    spec = info.get("input")
    declared: dict[str, Any] = {}
    for group in ("required", "optional"):
        values = spec.get(group) if isinstance(spec, Mapping) else None
        if isinstance(values, Mapping):
            declared.update(values)
    return declared


def _typed_inputs(info: Mapping[str, Any], wanted: str) -> list[str]:
    """Input names whose declared type is the scalar kind `wanted`.

    `/object_info` declares an input as `[type, options]`, where `type` is a
    string for scalars and a list for combos. Only scalars are role candidates:
    a combo is a fixed menu, and a linked slot has no widget to patch.
    """
    return [
        name
        for name, value in _declared_inputs(info).items()
        if isinstance(value, (list, tuple)) and value and value[0] == wanted
    ]


def _image_upload_inputs(info: Mapping[str, Any]) -> list[str]:
    """Input names that accept an uploaded image file.

    Separate from `_typed_inputs` because an upload widget's declared type is the
    *combo* of files already on the server, so no kind comparison can match it.
    """
    return [name for name, value in _declared_inputs(info).items() if is_image_upload(value)]


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
            "image_inputs": _image_upload_inputs(entry),
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
    graph, slots = resolve_graph(config, graph_id)
    # A graph with no negative slot silently discards everything the composer
    # routed to the negative -- removed outfits, turned-away faces. Disclose it on
    # the attachment rather than let the user wonder why the negation had no effect.
    if "negative" not in slots and request.negative_prompt.strip():
        notes = (*notes, "this workflow has no negative prompt input; negative prompt was not applied")
    client = _client(config)
    # Resolution happened above the engine (it reads conversation state); the upload
    # belongs here, where everything else that talks to ComfyUI lives. Distinct
    # digests only: two slots pointing at the same image are one file on the server.
    uploaded: dict[str, str] = {}
    for reference in request.references:
        if reference.digest not in uploaded:
            uploaded[reference.digest] = await client.upload_image(
                reference.data,
                reference.mime,
                digest=reference.digest,
                timeout=min(120.0, request.timeout_seconds),
                progress=progress,
            )
    patched, output_node = patch_graph(
        graph,
        slots,
        prompt=request.prompt,
        negative_prompt=request.negative_prompt,
        seed=request.seed,
        checkpoint=checkpoint,
        references=[(reference.slot, uploaded[reference.digest]) for reference in request.references],
    )
    result = await client.generate(
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
            # What a reroll re-fetches by: `origin` names the row or card the bytes
            # came from, so replay reproduces the *same* reference.
            "references": [
                {
                    "slot": list(r.slot),
                    "source": r.source,
                    "origin": r.origin,
                    "digest": r.digest,
                    "comfy_name": uploaded[r.digest],
                }
                for r in request.references
            ],
            # Record the model only when the graph actually applied it. A
            # self-contained graph (no checkpoint slot) ignores the value, and
            # replay reads a null here as "the graph carried its own model".
            "backend_model": checkpoint if "checkpoint" in slots else None,
            "notes": list(notes),
        },
    )
