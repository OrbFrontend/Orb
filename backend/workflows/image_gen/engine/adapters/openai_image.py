"""Cloud API image generation over the OpenAI-shaped `/images/*` contract.

One adapter, many providers: the wire dialect that genuinely differs -- dimension
spelling, model-list shape, reference encoding -- lives in `providers.py`'s preset
table, which is the single place a provider is declared and the single place it
gets corrected.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

from PIL import Image

from ...config import (
    MAX_REFERENCE_SLOTS,
    REFERENCE_SOURCES,
    style_reference_sources,
    style_source,
)
from ..contracts import (
    ImageBackendCapabilities,
    ImageRequest,
    ImageResult,
    ProgressCallback,
    RenderTarget,
    emit,
)
from ..openai_image_client import MODEL_NOT_FOUND, CloudImageError, OpenAIImageClient
from ..providers import (
    BuiltRequest,
    ProviderPreset,
    build_edit_body,
    build_generation_body,
    get_preset,
    reference_capacity,
    takes_references,
)
from .base import ImageAdapter, replayed_target, replayed_text

logger = logging.getLogger(__name__)

CLOUD_REFERENCE_MAX_BYTES = 4 * 1024 * 1024
CLOUD_REFERENCE_SLOT = ("cloud", "image_0")


def cloud_reference_slot(index: int) -> tuple[str, str]:
    """The synthetic slot key for the *index*-th cloud reference.

    Synthetic because a cloud provider has no node graph to key against, and stable
    because a stored reference is re-keyed by it on replay. Position 0 keeps the
    literal `CLOUD_REFERENCE_SLOT` spelling every attachment made before this backend
    could take more than one recorded.
    """
    return CLOUD_REFERENCE_SLOT if index == 0 else ("cloud", f"image_{index}")


def _recorded_sources(replay: Mapping[str, Any], capacity: int) -> list[str]:
    """The per-slot sources a stored render used, re-keyed onto this target's slots.

    The same rule the ComfyUI adapter applies, for the same reason: the sources moved
    onto the style, where they are editable after the fact, so a rehydrate replaying
    them off the style would reproduce a different picture. `[]` when the record names
    no cloud slot, and the caller falls back to the legacy single `reference_source`
    and then to the style.
    """
    entries = replay.get("references")
    recorded: dict[tuple[str, str], str] = {}
    for entry in entries if isinstance(entries, (list, tuple)) else ():
        slot = entry.get("slot") if isinstance(entry, Mapping) else None
        if isinstance(slot, (list, tuple)) and len(slot) == 2:
            recorded[(str(slot[0]), str(slot[1]))] = str(entry.get("source") or "")
    sources = [recorded.get(cloud_reference_slot(index), "") for index in range(capacity)]
    return sources if any(sources) else []


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


class OpenAICompatibleImageAdapter(ImageAdapter):
    source_id: ClassVar[str] = "cloud"
    display_name: ClassVar[str] = "Cloud API"
    capabilities: ClassVar[ImageBackendCapabilities] = CAPABILITIES

    @property
    def _cloud(self) -> Mapping[str, Any]:
        return self.config["cloud"]

    @property
    def _provider_id(self) -> str:
        """Which connection the bound style renders on.

        Off the style, not off `cloud["provider"]`: two styles on one config can name
        two providers, and the stored `provider` is only the legacy answer for a style
        that predates connection linking -- which `style_source` falls back to.
        """
        return style_source(self.config, self.style)[1]

    @property
    def _preset(self) -> ProviderPreset | None:
        return get_preset(self._provider_id)

    @property
    def _entry(self) -> Mapping[str, Any]:
        return self._cloud["providers"].get(self._provider_id) or {}

    @property
    def label(self) -> str:
        preset = self._preset
        return preset.label if preset else self.display_name

    def _base_url(self) -> str:
        return str(self._entry.get("base_url") or "") or (self._preset.base_url if self._preset else "")

    def _model(self) -> str:
        """The model the bound style names, or the provider's own default.

        The default is resolved here rather than written into the config, so
        relinking a style to a provider with a different default needs no rewrite --
        `""` keeps meaning "whatever this connection opens with".
        """
        preset = self._preset
        return str(self.style.get("model") or "") or (preset.default_model if preset else "")

    def readiness(self, model: str = "") -> dict:
        """The single statement of what this configuration is still missing.

        `model` overrides the configured one so a *replay* is judged on the model it
        recorded: clearing the model field in settings must not refuse a rehydrate
        of an image whose own model is still there to render it.
        """
        preset = self._preset
        if preset is None:
            return {
                "ready": False,
                "reason": "unknown_provider",
                "detail": f"Unknown image provider {self._provider_id!r}; pick one in settings",
            }
        if not self._base_url():
            return {
                "ready": False,
                "reason": "no_base_url",
                "detail": f"Enter the API base URL for {preset.label}",
            }
        if not str(self._entry.get("api_key") or ""):
            return {"ready": False, "reason": "no_api_key", "detail": f"Paste an API key for {preset.label}"}
        chosen = model or self._model()
        if not chosen:
            return {"ready": False, "reason": "no_model", "detail": f"Choose a model for {preset.label}"}
        return {"ready": True, "reason": "", "detail": f"{preset.label} — {chosen}"}

    def resolve_target(self, replay: Mapping[str, Any] | None) -> RenderTarget:
        preset = self._preset
        style = self.style
        model, width, height = replayed_target(
            replay, model=self._model(), width=int(style["width"]), height=int(style["height"])
        )
        quality = replayed_text(replay, "quality", str(style.get("quality") or ""))
        references: tuple[Mapping[str, Any], ...] = ()
        notes: list[str] = []
        # How many slots this backend has is a **provider** fact, not a scene fact:
        # `resolve_target` has no conversation access, deliberately, so the target
        # declares its capacity and the render fills what it has. The style stores a
        # list so a ComfyUI graph's several `LoadImage` widgets can each answer, and a
        # style relinked between the two keeps its answers either way -- so anything
        # past this capacity is stored but inert, and nothing may read it as intent.
        # Clamped to the same ceiling the picker and `normalize_config` enforce: a style
        # cannot store a source past `MAX_REFERENCE_SLOTS`, so declaring a slot past it
        # would declare one that is permanently Off and count against `unfilled`.
        capacity = reference_capacity(preset, MAX_REFERENCE_SLOTS) if preset is not None else 1
        sources = style_reference_sources(style)[:capacity]
        if replay:
            # Position 0 comes from the scalar `reference_source`, which is the
            # authoritative recorded fact for this backend and the only one an
            # attachment made before it could declare a second slot carries. The
            # per-reference records answer for the rest, which that scalar cannot
            # address. Precedence, not a merge: a record whose scalar is absent falls
            # back to the style at position 0 exactly as it always has.
            sources = [replayed_text(replay, "reference_source", next(iter(sources), ""))] + _recorded_sources(
                replay, capacity
            )[1:]
        source = next(iter(sources), "")
        # Whether this target can carry a reference *at all* -- a fact about the
        # provider's dialect, not about the model and not about what the style switched
        # on. A style with every row Off still has whatever ceiling the provider gives
        # it, and the caller needs that ceiling to tell "the style left a row Off" apart
        # from "there is no further row to turn on".
        #
        # Deliberately not asked of the model any more: whether *this* model reads a
        # reference is the model's to answer, at render time, by refusing. Declaring no
        # slot on the model's behalf is how a capability the user is paying for goes
        # missing with nothing on screen to say so.
        usable = preset is not None and takes_references(preset)
        if preset is not None and usable and any(name in REFERENCE_SOURCES for name in sources):
            # Enumerated before filtering: a style whose first row is Off and whose
            # second is on fills `image_1`, not `image_0`, or a replay would re-key
            # onto the wrong one.
            references = tuple(
                {
                    "slot": list(cloud_reference_slot(index)),
                    "source": name,
                    "label": "Reference image" if index == 0 else f"Reference image {index + 1}",
                    "mimes": list(preset.reference_mimes),
                    "max_bytes": CLOUD_REFERENCE_MAX_BYTES,
                    "required": False,
                }
                for index, name in enumerate(sources)
                if name in REFERENCE_SOURCES
            )
        return RenderTarget(
            source=self.source_id,
            target_id="",
            model=model,
            supports_negative_prompt=bool(preset and preset.supports_negative_prompt),
            supports_seed=bool(preset and preset.supports_seed),
            supports_dimensions=bool(preset and preset.dimension_mode != "none"),
            width=width,
            height=height,
            reference_slots=references,
            notes=tuple(notes),
            quality=quality,
            reference_source=source,
            reference_capacity=capacity if usable else 0,
        )

    def _client(self, timeout: float) -> OpenAIImageClient:
        return OpenAIImageClient(
            self._base_url(),
            str(self._entry.get("api_key") or ""),
            label=self.label,
            timeout=timeout,
        )

    def _require_preset(self) -> ProviderPreset:
        """Enough to reach the provider at all -- the discovery paths.

        Deliberately not full readiness: Test connection is what the user presses
        *before* choosing a model, because listing the models is what fills the
        picker. Gating it on a chosen model makes the picker unreachable.
        """
        state = self.readiness()
        return self._pass(state, blocked=state["reason"] in ("unknown_provider", "no_base_url"))

    def _require_ready(self, model: str) -> ProviderPreset:
        """Enough to render. Reached before a request is built, so a provider with no
        `default_model` -- AI/ML API and `custom` both ship none -- says "choose a
        model" instead of posting `model: ""` and relaying whatever the provider makes
        of it."""
        state = self.readiness(model)
        return self._pass(state, blocked=not state["ready"])

    def _pass(self, state: Mapping[str, Any], *, blocked: bool) -> ProviderPreset:
        preset = self._preset
        if preset is None or blocked:
            raise CloudImageError(str(state["detail"]), str(state["reason"]))
        return preset

    async def validate_connection(self, *, allow_cached: bool = False) -> dict:
        """Model discovery **only** -- this must never submit a generation.

        ComfyUI's shape (`{ok, capabilities, system, models}`), so the panel needs no
        change; `system.devices` is absent, which degrades its "Connected — <device>"
        line to a bare "Connected" rather than breaking it.
        """
        preset = self._require_preset()
        client = self._client(30.0)
        if preset.auth_probe_path:
            await client.verify_key(preset.auth_probe_path)
        return {
            "ok": True,
            "capabilities": dict(CAPABILITIES),
            "system": {"provider": preset.label, "host": urlsplit(self._base_url()).hostname or ""},
            "models": await _discover(client, preset),
        }

    async def list_models(self) -> list[str]:
        preset = self._require_preset()
        return await _discover(self._client(30.0), preset)

    async def generate(
        self,
        request: ImageRequest,
        *,
        target: RenderTarget,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        preset = self._require_ready(target.model)
        client = self._client(request.timeout_seconds)
        await emit(progress, "rendering", {"backend": self.label})

        async def submit(model: str):
            """One attempt, and the notes that building it produced."""
            built = self._build(preset, request, target, model=model)
            path = self._path(preset, request, model=model)
            image = await client.create_image(path, built.body, provider_id=preset.id, timeout=request.timeout_seconds)
            return image, built.notes

        model = target.model
        notes = list(target.notes)
        try:
            image, build_notes = await submit(model)
        except CloudImageError as exc:
            configured = self._model()
            if exc.kind != MODEL_NOT_FOUND or not configured or configured == model:
                raise
            notes.append(f"the model this image used ({model}) is gone; rendered with {configured} instead")
            model = configured
            image, build_notes = await submit(model)
        notes.extend(build_notes)

        width, height = await asyncio.to_thread(_probe_size, image.data)
        return ImageResult(
            image_bytes=image.data,
            mime=image.mime,
            backend_info={
                "source": self.source_id,
                "workflow_id": None,
                "backend_model": model,
                "provider": preset.id,
                "quality": target.quality,
                "reference_source": target.reference_source,
                "width": width,
                "height": height,
                "size_measured": width is not None,
                "steps": None,
                "cfg": None,
                "sampler": None,
                "scheduler": None,
                "seed_honored": target.supports_seed,
                "cost": image.cost,
                "references": [reference.record() for reference in request.references],
                "notes": notes,
            },
        )

    def _path(self, preset: ProviderPreset, request: ImageRequest, *, model: str) -> str:
        """Where this render posts.

        References ride the edits endpoint where one exists and the ordinary
        generations body where it does not -- Together has no `/images/edits` and
        still takes them. Derived from the same condition `_build` uses, so a body
        that carries no reference can never be posted to an endpoint that requires
        one.
        """
        if request.references and preset.edits_path and takes_references(preset):
            return preset.edits_path
        return preset.generations_path

    def _build(self, preset: ProviderPreset, request: ImageRequest, target: RenderTarget, *, model: str) -> BuiltRequest:
        common = {
            "model": model,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seed": request.seed if target.supports_seed else None,
            "width": target.width,
            "height": target.height,
            "quality": target.quality,
            "n": 1,
        }
        references = request.references if takes_references(preset) else ()
        if references:
            return build_edit_body(preset, references=references, **common)
        return build_generation_body(preset, **common)


async def _discover(client: OpenAIImageClient, preset: ProviderPreset) -> list[str]:
    """Which endpoint to ask and which shape to read it as is entirely a preset fact.

    Unpacked here rather than inside the client, which is deliberately ignorant of
    `providers.py` -- but unpacked in *one* place, so Test connection and the model
    picker can never ask two different questions.
    """
    return await client.list_models(preset.models_path, preset.models_response, preset.models_filter)


def _probe_size(data: bytes) -> tuple[int | None, int | None]:
    """The real pixel dimensions of the returned image.

    Keeps a cloud attachment's record the same shape as ComfyUI's, and is what
    lets a later rehydrate replay the size it was generated at.
    """
    try:
        with Image.open(io.BytesIO(data)) as probe:
            return probe.size[0], probe.size[1]
    except Exception:
        logger.warning("could not read the dimensions of the image returned by the cloud provider", exc_info=True)
        return None, None
