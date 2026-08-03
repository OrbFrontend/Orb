"""External ComfyUI generation adapter."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from ...config import DEFAULT_CLOUD_EDGE, REFERENCE_MIMES, style_reference_sources
from ..comfy_client import ComfyClient
from ..contracts import (
    ImageBackendCapabilities,
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    RenderTarget,
)
from ..graph import (
    declared_inputs,
    describe_render_params,
    enabled_references,
    has_graph,
    is_image_upload,
    patch_graph,
    reference_slots,
    resolve_graph,
    validate_graph_structure,
)
from .base import ImageAdapter, replayed_target

COMFY_REFERENCE_MAX_BYTES = 8 * 1024 * 1024

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
    "supports_negative_prompt": True,
    "supports_seed": True,
    "supports_dimensions": True,
    "supports_references": True,
}


class ExternalComfyAdapter(ImageAdapter):
    source_id: ClassVar[str] = "external_comfy"
    display_name: ClassVar[str] = "External ComfyUI"
    capabilities: ClassVar[ImageBackendCapabilities] = CAPABILITIES

    def _graphs(self) -> Sequence[Mapping[str, Any]]:
        return self.config["external_comfy"]["user_graphs"]

    def readiness(self, model: str = "") -> dict:
        """Whether the style this adapter is bound to can render, not whether every
        style can.

        `model` is ignored: a ComfyUI render is pinned by its graph, whose checkpoint
        is a node inside it rather than a field a caller can substitute.

        Auditing the whole list would read as a permanently stuck "Setup required":
        a cloud-linked style will never have a workflow, and a just-added style is
        not finished yet, and neither says anything about the next Visualize. The
        bound style is what makes that a *choice* rather than a limitation -- ask the
        question about another style and this answers about that one.
        """
        config = self.config
        graphs = {graph["id"]: graph for graph in self._graphs()}
        style = self.style
        label = style["label"] or style["id"]
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
        if not style["checkpoint"] and "checkpoint" in graphs[style["workflow"]]["slots"]:
            return {
                "ready": False,
                "reason": "no_checkpoint",
                "detail": f"Choose a checkpoint for {label!r} before generating",
            }
        return {"ready": True, "reason": "", "detail": f"External ComfyUI at {config['external_comfy']['api_url']}"}

    def _graph_slots(self, graph_id: str) -> Mapping[str, Any]:
        """`graph_id`'s slot map, or an empty one when it no longer resolves.

        Which optional roles a graph maps is what the RenderTarget's dynamic tier
        answers about, so both questions -- negative prompt, output size -- read the
        same map rather than each walking the list its own way.
        """
        return next((item["slots"] for item in self._graphs() if item["id"] == graph_id), {})

    def _reference_slots(self, slots: Mapping[str, Any], sources: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
        """The slots `sources` has switched on, each carrying the policy ComfyUI imposes.

        The graph declares which of its inputs load an image; the style says where each
        one draws from, so the same workflow can run under one style that feeds it the
        previous image and another that renders it from the prompt alone.

        `mimes` is load-bearing: the upload names the file by extension off the mime,
        so anything outside the three Orb declares lands on the server as a `.png`
        that is not one. `required` is True of the slots that survive this pairing --
        something asked for an image there, and rendering it unfilled would submit the
        exporter's own filename and draw whatever that machine had.
        """
        return tuple(
            {
                **copy.deepcopy(entry),
                "source": source,
                "mimes": list(REFERENCE_MIMES),
                "max_bytes": COMFY_REFERENCE_MAX_BYTES,
                "required": True,
            }
            for entry, source in enabled_references(slots, sources)
        )

    def resolve_target(self, replay: Mapping[str, Any] | None) -> RenderTarget:
        style = self.style
        graph_id = style["workflow"]
        notes: list[str] = []
        if replay:
            stored_graph = replay.get("workflow_id")
            recorded = stored_graph if isinstance(stored_graph, str) and stored_graph else ""
            if recorded and not has_graph(self.config, recorded):
                notes.append(
                    f"the workflow this image used ({recorded}) is gone; rendered with {graph_id} instead"
                    if graph_id
                    else f"the workflow this image used ({recorded}) is gone, and this style has no workflow assigned"
                )
                recorded = ""
            graph_id = recorded or graph_id
        checkpoint, width, height = replayed_target(
            replay, model=style["checkpoint"], width=int(style["width"]), height=int(style["height"])
        )
        slots = self._graph_slots(graph_id)
        # The style is the live answer; a replay is about a render that already happened,
        # and the sources have been the style's to edit since. `or` rather than a branch:
        # a record that re-keys onto nothing (the graph was replaced, or the image
        # predates the record) is no answer, and the style is the better guess.
        sources = style_reference_sources(style)
        if replay:
            sources = _recorded_sources(slots, replay) or sources
        sized = "width" in slots and "height" in slots
        if not sized and (width, height) != (DEFAULT_CLOUD_EDGE, DEFAULT_CLOUD_EDGE):
            notes.append("this workflow has no resolution inputs mapped; it decides its own output size")
        return RenderTarget(
            source=self.source_id,
            target_id=graph_id,
            model=checkpoint,
            supports_negative_prompt="negative" in slots,
            supports_seed=True,
            supports_dimensions=sized,
            width=width if sized else None,
            height=height if sized else None,
            reference_slots=self._reference_slots(slots, sources),
            notes=tuple(notes),
        )

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
        # The sources ride along because they decide which image widgets Orb overwrites,
        # and so which of them still have to name a file this server already has. Two
        # styles on one workflow that answer that differently are two selections -- and
        # the first style to reach one names it, because a config-wide check that says
        # only "Node 11 needs image 'x.jpeg'" leaves the user no way to tell which style
        # to go and fix.
        selections: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for style in config["styles"]:
            if style["workflow"]:
                key = (style["workflow"], style["checkpoint"], tuple(style_reference_sources(style)))
                selections.setdefault(key, style["label"] or style["id"])
        models: list[str] | None = None

        async def available_checkpoints() -> list[str]:
            nonlocal models
            if models is None:
                models = await client.models("checkpoints")
            return models

        for (graph_id, checkpoint, sources), label in selections.items():
            try:
                graph, slots = resolve_graph(config, graph_id)
                if "checkpoint" in slots:
                    graph, _ = patch_graph(
                        graph,
                        slots,
                        prompt="connection test",
                        negative_prompt="",
                        seed=0,
                        checkpoint=checkpoint,
                    )
                filled = [entry for entry, _ in enabled_references(slots, sources)]
                validate_graph_structure(graph, slots, info, filled=filled)
            except ImageGenerationError as exc:
                raise ImageGenerationError(f"Style {label!r}: {exc}") from exc
        try:
            discovered = await available_checkpoints()
        except ImageGenerationError:
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
                "dimension_inputs": [name for name in _typed_inputs(entry, "INT") if name.lower() in ("width", "height")],
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
        if "negative" not in slots and request.negative_prompt.strip():
            notes = (*notes, "this workflow has no negative prompt input; negative prompt was not applied")
        client = self._client()
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
            width=target.width,
            height=target.height,
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
                "backend_model": target.model if "checkpoint" in slots else None,
                "seed_honored": True,
                "notes": list(notes),
            },
        )


def _slot_key(slot: Any) -> tuple[str, str] | None:
    return (str(slot[0]), str(slot[1])) if isinstance(slot, (list, tuple)) and len(slot) == 2 else None


def _recorded_sources(slots: Mapping[str, Any], replay: Mapping[str, Any]) -> list[str]:
    """The per-slot sources the stored render used, re-keyed onto this graph's slots.

    Rehydrate reproduces the stored render target. The sources moved onto the style,
    where they can be edited after the fact, so replaying them off the style would
    quietly reproduce a *different* picture -- the one failure a rehydrate is not
    allowed to have. The record names the node input each reference filled, and that
    re-keys onto the declared list directly.

    `[]` when nothing recorded lands on a declared slot, which is a record about some
    other graph; the caller falls back to the style rather than rendering blind.
    """
    entries = replay.get("references")
    recorded: dict[tuple[str, str], str] = {}
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        key = _slot_key(entry.get("slot")) if isinstance(entry, Mapping) else None
        if key is not None:
            recorded[key] = str(entry.get("source") or "")
    sources: list[str] = []
    for entry in reference_slots(slots):
        key = _slot_key(entry.get("slot"))
        sources.append(recorded.get(key, "") if key is not None else "")
    return sources if any(sources) else []


def _safe_system_summary(stats: Mapping[str, Any]) -> dict:
    """The two facts Orb shows off `/system_stats`, bounded and nothing else copied.

    An allowlist rather than a filter: this payload reaches the settings panel, and
    a ComfyUI build that starts reporting paths or usernames must not carry them
    along on the strength of nobody having thought to exclude them.
    """
    system = stats.get("system")
    devices = stats.get("devices")
    return {
        "comfyui_version": str((system if isinstance(system, Mapping) else {}).get("comfyui_version", ""))[:80],
        "devices": [
            {"name": str(device.get("name", ""))[:160], "vram_total": device.get("vram_total")}
            for device in (devices if isinstance(devices, list) else [])
            if isinstance(device, Mapping)
        ],
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
