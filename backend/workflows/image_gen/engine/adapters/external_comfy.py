"""External ComfyUI generation adapter."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from ...config import REFERENCE_MIMES, active_style
from ..comfy_client import ComfyClient
from ..contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
)
from ..graph import (
    declared_inputs,
    describe_render_params,
    has_graph,
    is_image_upload,
    patch_graph,
    reference_slots,
    resolve_graph,
    validate_graph_structure,
)
from ..target import RenderTarget
from .base import ImageAdapter

# Generous next to the cloud adapter's base64-in-JSON cap, because this is a
# multipart upload -- but still declared, so both backends answer in one place.
COMFY_REFERENCE_MAX_BYTES = 8 * 1024 * 1024

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
    # Per-*graph* facts here, not per-backend: the static answer is "this backend
    # can express them", and what one graph honours is on the RenderTarget.
    "supports_negative_prompt": True,
    "supports_seed": True,
    "supports_dimensions": False,
    "supports_references": True,
}


class ExternalComfyAdapter(ImageAdapter):
    source_id: ClassVar[str] = "external_comfy"
    display_name: ClassVar[str] = "External ComfyUI"
    capabilities: ClassVar[ImageBackendCapabilities] = CAPABILITIES

    # ── configuration-only answers, no network I/O ────────────────────────────

    def _graphs(self) -> Sequence[Mapping[str, Any]]:
        return self.config["external_comfy"]["user_graphs"]

    def readiness(self, model: str = "") -> dict:
        """Whether the style about to render can, not whether every style can.

        `model` is ignored: a ComfyUI render is pinned by its graph, whose checkpoint
        is a node inside it rather than a field a caller can substitute.

        Auditing the whole list would read as a permanently stuck "Setup required":
        a cloud-linked style will never have a workflow, and a just-added style is
        not finished yet, and neither says anything about the next Visualize.
        Per-style problems stay visible -- the panel marks the row, and selecting
        that style in the card asks this question again about it.
        """
        config = self.config
        graphs = {graph["id"]: graph for graph in self._graphs()}
        style = active_style(config)
        label = style["label"] or style["id"]
        # External mode ships no default graph, so nothing pinned cannot render at
        # all -- the first thing to fix, ahead of checkpoints.
        if not style["workflow"]:
            return {
                "ready": False,
                "reason": "no_workflow",
                "detail": f"Import a ComfyUI workflow and assign it to {label!r}",
            }
        if style["workflow"] not in graphs:
            return {
                "ready": False,
                "reason": "unknown_workflow",
                "detail": f"{label!r} names a workflow that is not imported: {style['workflow']}",
            }
        # A checkpoint is only required when the pinned graph exposes a model slot for
        # Orb's selection to override; a self-contained graph carries its own.
        if not style["checkpoint"] and "checkpoint" in graphs[style["workflow"]]["slots"]:
            return {
                "ready": False,
                "reason": "no_checkpoint",
                "detail": f"Choose a checkpoint for {label!r} before generating",
            }
        return {"ready": True, "reason": "", "detail": f"External ComfyUI at {config['external_comfy']['api_url']}"}

    def _graph_has_negative(self, graph_id: str) -> bool:
        """Whether `graph_id` maps a negative-prompt slot. A graph without one
        discards the composed negative, so the composer is told to leave `avoid`
        empty rather than spend effort on a negation the workflow throws away."""
        return any(item["id"] == graph_id and "negative" in item["slots"] for item in self._graphs())

    def _graph_reference_slots(self, graph_id: str) -> tuple[Mapping[str, Any], ...]:
        """This graph's mapped slots, each carrying the policy ComfyUI imposes.

        `mimes` is load-bearing: the upload names the file by extension off the mime,
        so anything outside the three Orb declares lands on the server as a `.png`
        that is not one. `required` is True because an unfilled `LoadImage` submits
        the exporter's own filename and draws whatever that machine had there.
        """
        for item in self._graphs():
            if item["id"] == graph_id:
                return tuple(
                    {
                        **copy.deepcopy(entry),
                        "mimes": list(REFERENCE_MIMES),
                        "max_bytes": COMFY_REFERENCE_MAX_BYTES,
                        "required": True,
                    }
                    for entry in reference_slots(item["slots"])
                )
        return ()

    def resolve_target(self, style: Mapping[str, Any], replay: Mapping[str, Any] | None) -> RenderTarget:
        graph_id = style["workflow"]
        checkpoint = style["checkpoint"]
        notes: list[str] = []
        if replay:
            stored_graph = replay.get("workflow_id")
            recorded = stored_graph if isinstance(stored_graph, str) and stored_graph else ""
            if recorded and not has_graph(self.config, recorded):
                notes.append(
                    f"the workflow this image used ({recorded}) is gone; rendered with {graph_id!r} instead"
                    if graph_id
                    else f"the workflow this image used ({recorded}) is gone, and this style has no workflow assigned"
                )
                recorded = ""
            stored_checkpoint = replay.get("backend_model")
            # Empty means the original ran a graph carrying its own loaders, so fall
            # through rather than invent a pin it never had.
            if isinstance(stored_checkpoint, str) and stored_checkpoint:
                checkpoint = stored_checkpoint
            graph_id = recorded or graph_id
        return RenderTarget(
            source=self.source_id,
            target_id=graph_id,
            model=checkpoint,
            supports_negative_prompt=self._graph_has_negative(graph_id),
            supports_seed=True,
            # The graph carries its own latent size; Orb never picks one.
            supports_dimensions=False,
            width=None,
            height=None,
            reference_slots=self._graph_reference_slots(graph_id),
            notes=tuple(notes),
        )

    # ── network ───────────────────────────────────────────────────────────────

    def _client(self) -> ComfyClient:
        ext = self.config["external_comfy"]
        return ComfyClient(ext["api_url"], ext["api_key"])

    async def validate_connection(self, *, allow_cached: bool = False) -> dict:
        """Prove this configuration can render, without submitting anything.

        `allow_cached` lets the readiness probe reuse a recent node catalogue; an
        explicit Test connection leaves it False, because pressing it means "look
        again".
        """
        config = self.config
        client = self._client()
        stats = await client.system_stats()
        info = await client.object_info(allow_cached=allow_cached)
        checked: set[str] = set()
        # Only pinned workflows: a style with none has no graph to validate, and
        # readiness reports that gap separately.
        selections = [(s["workflow"], s["checkpoint"]) for s in config["styles"] if s["workflow"]]
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
            # Override the model before validating, so Test connection checks what
            # will actually run rather than the filename the graph was imported with.
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

    async def list_models(self) -> list[str]:
        return await self._client().models("checkpoints")

    async def node_roles(self, class_types: Sequence[str]) -> dict:
        """Which inputs of the named node classes can carry which slot role.

        Deliberately **not** on the ABC: ComfyUI-only, and the importer that needs it
        stays usable while another source is selected. The typing rule lives here,
        next to the validation using the same catalogue, so only the verdict crosses
        the wire -- `/object_info` is tens of megabytes. Unknown classes are absent
        from the result and the picker degrades to its name-based fallback.
        """
        info = await self._client().object_info(allow_cached=True)
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
        self,
        request: ImageRequest,
        *,
        target: RenderTarget,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        graph, slots = resolve_graph(self.config, target.target_id)
        notes = target.notes
        # A graph with no negative slot discards everything the composer routed to
        # the negative, so say so rather than let the user wonder why it had no effect.
        if "negative" not in slots and request.negative_prompt.strip():
            notes = (*notes, "this workflow has no negative prompt input; negative prompt was not applied")
        client = self._client()
        # Distinct digests only: two slots pointing at the same image are one file
        # on the server.
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
            checkpoint=target.model,
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
                "source": self.source_id,
                "workflow_id": target.target_id,
                "references": [{**r.record(), "comfy_name": uploaded[r.digest]} for r in request.references],
                # Recorded only when the graph actually applied it: a self-contained
                # graph ignores the value, and replay reads null as "its own model".
                "backend_model": target.model if "checkpoint" in slots else None,
                "seed_honored": True,
                "notes": list(notes),
            },
        )


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


def _typed_inputs(info: Mapping[str, Any], wanted: str) -> list[str]:
    """Input names whose declared type is the scalar kind `wanted`.

    `/object_info` declares an input as `[type, options]`, `type` being a string for
    scalars and a list for combos. Only scalars are role candidates: a combo is a
    fixed menu, and a linked slot has no widget to patch.
    """
    return [
        name
        for name, value in declared_inputs(info).items()
        if isinstance(value, (list, tuple)) and value and value[0] == wanted
    ]


def _image_upload_inputs(info: Mapping[str, Any]) -> list[str]:
    """Input names that accept an uploaded image file. Separate from `_typed_inputs`
    because an upload widget's declared type is the *combo* of files already on the
    server, so no kind comparison can match it."""
    return [name for name, value in declared_inputs(info).items() if is_image_upload(value)]
