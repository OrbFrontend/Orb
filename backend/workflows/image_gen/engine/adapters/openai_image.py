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

from ...config import REFERENCE_SOURCES
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
    takes_references,
)
from .base import ImageAdapter

logger = logging.getLogger(__name__)

# Base64 inflates by 4/3 *inside a JSON body*, where ComfyUI's path is multipart,
# so a bound that is fine for an upload is not fine here.
CLOUD_REFERENCE_MAX_BYTES = 4 * 1024 * 1024
# The synthetic slot every cloud reference rides. Keeping the `(node, field)` shape
# lets references.py, the stored record and reroll's style-changed logic stay
# unchanged; a nullable `slot` would ripple through all three.
CLOUD_REFERENCE_SLOT = ("cloud", "image_0")
# Said in two places -- when the slot is withheld up front, and again when a degrade
# substitutes a model that cannot take one -- so the two cannot drift into two
# spellings of one fact.
_NO_REFERENCES = "{model} does not accept reference images, so none was sent"

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
    # The static tier answers for the *class* of backend. Whether one provider
    # honours a negative prompt or a seed is a preset fact on the RenderTarget.
    "supports_negative_prompt": True,
    "supports_seed": True,
    "supports_dimensions": True,
    "supports_references": True,
}


class OpenAICompatibleImageAdapter(ImageAdapter):
    source_id: ClassVar[str] = "cloud"
    display_name: ClassVar[str] = "Cloud API"
    capabilities: ClassVar[ImageBackendCapabilities] = CAPABILITIES

    # ── configuration ─────────────────────────────────────────────────────────

    @property
    def _cloud(self) -> Mapping[str, Any]:
        return self.config["cloud"]

    @property
    def _preset(self) -> ProviderPreset | None:
        return get_preset(str(self._cloud["provider"]))

    @property
    def _entry(self) -> Mapping[str, Any]:
        return self._cloud["providers"].get(self._cloud["provider"]) or {}

    @property
    def label(self) -> str:
        preset = self._preset
        return preset.label if preset else self.display_name

    def _base_url(self) -> str:
        return str(self._entry.get("base_url") or "") or (self._preset.base_url if self._preset else "")

    def _model(self) -> str:
        return str(self._entry.get("model") or "") or (self._preset.default_model if self._preset else "")

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
                "detail": f"Unknown image provider {self._cloud['provider']!r}; pick one in settings",
            }
        if not self._base_url():
            # `custom` ships none by design; without this the config reads ready and
            # then fails at render with nothing the user can act on.
            return {
                "ready": False,
                "reason": "no_base_url",
                "detail": f"Enter the API base URL for {preset.label}",
            }
        if not str(self._entry.get("api_key") or ""):
            return {"ready": False, "reason": "no_api_key", "detail": f"Paste an API key for {preset.label}"}
        chosen = model or self._model()
        if not chosen:
            # "a model for X", never "a X model": the labels start with every letter
            # ("a OpenAI", "a ElectronHub", "a xAI (Grok)"), and an article that reads
            # correctly for all of them is a per-label table for one word of grammar.
            return {"ready": False, "reason": "no_model", "detail": f"Choose a model for {preset.label}"}
        return {"ready": True, "reason": "", "detail": f"{preset.label} — {chosen}"}

    def resolve_target(self, style: Mapping[str, Any], replay: Mapping[str, Any] | None) -> RenderTarget:
        preset = self._preset
        cloud = self._cloud
        model = self._model()
        # Dimensions ride the target for the same reason `model` does: reading
        # config["cloud"]["width"] at render time would hand a *rehydrate* today's
        # setting, so an image made at 1024x1024 comes back 1536x1024 after the
        # picker moved -- the exact substitution rehydrate exists to avoid.
        width, height = int(cloud["width"]), int(cloud["height"])
        if replay:
            stored_model = replay.get("backend_model")
            if isinstance(stored_model, str) and stored_model:
                model = stored_model
            stored_w, stored_h = replay.get("width"), replay.get("height")
            if isinstance(stored_w, int) and isinstance(stored_h, int) and stored_w > 0 and stored_h > 0:
                width, height = stored_w, stored_h
        references: tuple[Mapping[str, Any], ...] = ()
        notes: list[str] = []
        source = str(cloud.get("reference_source") or "")
        # Gated on the capability, not on `edits_path`: Together has no
        # `/images/edits` and still takes references, on the generations body.
        if preset is not None and preset.supports_references and source in REFERENCE_SOURCES:
            if not takes_references(preset, model):
                # No slot is what stops the reference being resolved, encoded and
                # sent to a model that cannot use it. Withholding it rather than
                # letting the provider answer matters because the answer is not
                # uniform: Together's FLUX.2 rejects `image_url` outright, while its
                # schnell default returns 200 having quietly ignored it -- a paid
                # render with no reference and nothing to say so.
                notes.append(_NO_REFERENCES.format(model=model))
            else:
                references = (
                    {
                        "slot": list(CLOUD_REFERENCE_SLOT),
                        "source": source,
                        "label": "Reference image",
                        # Read per-entry by references.py: a provider taking PNG/JPEG
                        # must not be handed the WebP every render is stored as.
                        "mimes": list(preset.reference_mimes),
                        "max_bytes": CLOUD_REFERENCE_MAX_BYTES,
                        # Optional, unlike a ComfyUI graph slot: the same model has a
                        # plain generations endpoint one field away, so a first
                        # Visualize in an imageless chat renders from the prompt.
                        "required": False,
                    },
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
        )

    # ── network ───────────────────────────────────────────────────────────────

    def _client(self, timeout: float) -> OpenAIImageClient:
        return OpenAIImageClient(
            self._base_url(),
            str(self._entry.get("api_key") or ""),
            label=self.label,
            timeout=timeout,
        )

    # Both gates read `readiness()` rather than restating its sentences, so there is
    # one place a missing-configuration message is worded. They differ in *how much*
    # they demand, which is not the same question:
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
        `default_model` -- OpenRouter, Chutes, AI/ML API, ElectronHub and `custom` all
        ship none -- says "choose a model" instead of posting `model: ""` and relaying
        whatever the provider makes of it."""
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
            # First, so a bad key fails as "the API key was rejected" rather than
            # succeeding: NanoGPT serves its model catalogue to anonymous callers, so
            # the list below proves the provider is up and nothing about the key.
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
        # Synchronous API: one event at submit, no polling loop to report from.
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
            # The cloud analogue of `unknown_workflow`: the recorded model is gone,
            # so re-render on the configured one and disclose it. A 404 costs
            # nothing, and refusing surfaces only as a generic 500.
            if exc.kind != MODEL_NOT_FOUND or not configured or configured == model:
                raise
            notes.append(f"the model this image used ({model}) is gone; rendered with {configured} instead")
            # The substitute is not the model the slot was offered for: replaying a
            # Kontext image after the connection moved to a text-to-image model would
            # otherwise re-send `image_url` to something that cannot use it, and a
            # degrade meant to rescue the render would drop the reference in silence.
            if request.references and not takes_references(preset, configured):
                notes.append(_NO_REFERENCES.format(model=configured))
            model = configured
            # Only the *surviving* attempt's build notes are kept. Collecting the
            # failed one's too printed "the prompt was truncated" twice on any
            # degrade, since both builds truncate identically.
            image, build_notes = await submit(model)
        notes.extend(build_notes)

        width, height = await asyncio.to_thread(_probe_size, image.data)
        return ImageResult(
            image_bytes=image.data,
            mime=image.mime,
            backend_info={
                "source": self.source_id,
                # ComfyUI's key names, so existing attachments and the replay path
                # need no migration. There is no graph, so no id.
                "workflow_id": None,
                "backend_model": model,
                "provider": preset.id,
                # Probed off the returned image, not echoed from the request: an
                # aspect-only provider decides the actual size.
                "width": width,
                "height": height,
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
        if request.references and preset.edits_path and takes_references(preset, model):
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
            "quality": str(self._cloud.get("quality") or ""),
            # Always one: Orb stores one image per attachment, and `n` is the field
            # that silently multiplies the bill.
            "n": 1,
        }
        # Both builders are strict allowlists over the same preset, so anything the
        # provider does not declare is dropped there rather than guarded here. The
        # model is the one thing a preset cannot answer alone: `resolve_target`
        # already withholds the slot, and this keeps that true for the substituted
        # model on the degrade path.
        references = request.references if takes_references(preset, model) else ()
        if references:
            return build_edit_body(preset, references=references, **common)
        return build_generation_body(preset, **common)


async def _discover(client: OpenAIImageClient, preset: ProviderPreset) -> list[str]:
    """Which endpoint to ask and which shape to read it as is entirely a preset fact.

    Unpacked here rather than inside the client, which is deliberately ignorant of
    `providers.py` -- but unpacked in *one* place, so Test connection and the model
    picker can never ask two different questions.
    """
    return await client.list_models(preset.models_path, preset.models_response, preset.models_type_filter)


def _probe_size(data: bytes) -> tuple[int | None, int | None]:
    """The real pixel dimensions of the returned image.

    Keeps a cloud attachment's record the same shape as ComfyUI's, and is what
    lets a later rehydrate replay the size it was generated at.
    """
    try:
        with Image.open(io.BytesIO(data)) as probe:
            return probe.size[0], probe.size[1]
    except Exception:
        # Degrades rather than raises -- an unreadable header is no reason to throw
        # away an image the user already paid for. Logged because the cost is a
        # rehydrate that cannot pin its resolution, and silence made that look like
        # a provider that reports no size rather than a probe that failed.
        logger.warning("could not read the dimensions of the image returned by the cloud provider", exc_info=True)
        return None, None
