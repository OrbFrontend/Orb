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

# A multipart upload, so the ceiling is generous next to the cloud adapter's
# base64-in-JSON one -- but it is still declared rather than left implicit, so
# both backends answer the same question in the same place.
COMFY_REFERENCE_MAX_BYTES = 8 * 1024 * 1024

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
    # All four are per-*graph* facts here, not per-backend ones: a graph decides
    # whether it has a negative encoder, carries its own resolution, and exposes a
    # mapped LoadImage. The static answer is "this backend can express all of
    # them"; what one graph will actually honour is on the RenderTarget.
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

    def readiness(self) -> dict:
        """Whether the style about to render can render, not whether every style can.

        Auditing the whole style list was right while ComfyUI was the only backend
        and every style therefore needed a graph. It is wrong now, in two ways that
        both surface as a permanently stuck "Setup required": a style linked to a
        cloud connection will never have a workflow, and a style the user has just
        added has not been finished yet. Neither says anything about whether the
        next Visualize will work.

        Per-style problems are still visible -- the settings panel marks the row,
        and selecting that style in the card asks this question again about it.
        """
        config = self.config
        graphs = {graph["id"]: graph for graph in self._graphs()}
        style = active_style(config)
        label = style["label"] or style["id"]
        # External mode ships no default graph, so a style with nothing pinned cannot
        # render at all -- the first thing to fix, ahead of checkpoints.
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
        """Whether `graph_id` maps a negative-prompt slot.

        A graph with no negative slot discards the composed negative at render
        time, so the composer is told to leave `avoid` empty rather than spend the
        model's effort on a negation the workflow throws away.
        """
        return any(item["id"] == graph_id and "negative" in item["slots"] for item in self._graphs())

    def _graph_reference_slots(self, graph_id: str) -> tuple[Mapping[str, Any], ...]:
        """This graph's mapped slots, each carrying the policy ComfyUI imposes.

        `mimes` is not decoration: the upload names the file by extension off the
        mime (`_UPLOAD_EXTENSIONS`), so anything outside the three Orb declares
        would land on the server as a `.png` that is not one. `required` is True
        because a graph built around a `LoadImage` cannot render without it -- an
        unfilled slot submits the exporter's own filename and draws whatever that
        machine had at that path.
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
            # An empty stored checkpoint means the original ran a user graph carrying
            # its own loaders, where the value is ignored anyway -- fall through rather
            # than inventing a pin the original never had.
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

        `allow_cached` lets the Visualize modal's readiness probe reuse a recent
        node catalogue; an explicit Test connection leaves it False so the user sees
        the server as it is right now.
        """
        config = self.config
        client = self._client()
        stats = await client.system_stats()
        info = await client.object_info(allow_cached=allow_cached)
        checked: set[str] = set()
        # A style with no workflow assigned has no graph to validate against the
        # server; readiness reports that gap separately, so skip it here rather than
        # fail the whole probe. Only pinned workflows are checked.
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

    async def list_models(self) -> list[str]:
        return await self._client().models("checkpoints")

    async def node_roles(self, class_types: Sequence[str]) -> dict:
        """Which inputs of the named node classes can carry which slot role.

        Deliberately **not** on the ABC: this is ComfyUI-only, and the graph
        importer that needs it stays usable while another source is selected.

        The importer needs `/object_info` typing to build its slot picker, but that
        payload is tens of megabytes -- far too large to hand a browser just to
        populate four dropdowns. So the typing rule lives here, next to the
        validation that uses the same catalogue, and only the verdict crosses the
        wire. Unknown classes are simply absent from the result; the picker degrades
        to its name-based fallback for those.
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
        # A graph with no negative slot silently discards everything the composer
        # routed to the negative -- removed outfits, turned-away faces. Disclose it on
        # the attachment rather than let the user wonder why the negation had no effect.
        if "negative" not in slots and request.negative_prompt.strip():
            notes = (*notes, "this workflow has no negative prompt input; negative prompt was not applied")
        client = self._client()
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
                # What a reroll re-fetches by: `origin` names the row or card the bytes
                # came from, so replay reproduces the *same* reference. `source_digest`
                # is what proves it still holds the same picture -- see ResolvedReference.
                "references": [
                    {
                        "slot": list(r.slot),
                        "source": r.source,
                        "origin": r.origin,
                        "digest": r.digest,
                        "source_digest": r.source_digest,
                        "comfy_name": uploaded[r.digest],
                    }
                    for r in request.references
                ],
                # Record the model only when the graph actually applied it. A
                # self-contained graph (no checkpoint slot) ignores the value, and
                # replay reads a null here as "the graph carried its own model".
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
