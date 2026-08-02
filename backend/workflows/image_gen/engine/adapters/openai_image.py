"""Cloud API image generation over the OpenAI-shaped `/images/*` contract.

One adapter, many providers: the wire dialect that genuinely differs -- dimension
spelling, model-list shape, reference encoding -- lives in `providers.py`'s preset
table, which is the single place a provider is declared and the single place it
gets corrected.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

from ...config import REFERENCE_SOURCES
from ..contracts import (
    ImageBackendCapabilities,
    ImageRequest,
    ImageResult,
    ProgressCallback,
)
from ..openai_image_client import MODEL_NOT_FOUND, CloudImageError, OpenAIImageClient
from ..providers import (
    ProviderPreset,
    build_edit_body,
    build_generation_body,
    get_preset,
)
from ..target import RenderTarget
from .base import ImageAdapter

# Base64 inflates by 4/3 *inside a JSON body*, where ComfyUI's path is multipart --
# so a reference bound that is fine for an upload is not fine here.
CLOUD_REFERENCE_MAX_BYTES = 4 * 1024 * 1024
# The synthetic slot every cloud reference rides. Keeping the `(node, field)` shape
# is what lets `references.py`, `refetch_references`, the stored
# `{"slot","source","origin","digest"}` record and reroll's style-changed logic stay
# byte-identical; making `slot` nullable would ripple through all four.
CLOUD_REFERENCE_SLOT = ("cloud", "image_0")

CAPABILITIES: ImageBackendCapabilities = {
    "can_generate": True,
    "can_list_models": True,
    "can_install_curated_models": False,
    "managed_runtime": False,
    # The static tier answers for the *class* of backend. Whether the selected
    # provider honours a negative prompt or a seed is a preset fact and lands on
    # the RenderTarget; the settings panel states it as a permanent gap.
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

    def readiness(self) -> dict:
        preset = self._preset
        if preset is None:
            return {
                "ready": False,
                "reason": "unknown_provider",
                "detail": f"Unknown image provider {self._cloud['provider']!r}; pick one in settings",
            }
        if not self._base_url():
            # `custom` ships no base URL by design. Without this the config reads
            # ready and then fails at render with nothing the user can act on.
            return {
                "ready": False,
                "reason": "no_base_url",
                "detail": f"Enter the API base URL for {preset.label}",
            }
        if not str(self._entry.get("api_key") or ""):
            return {"ready": False, "reason": "no_api_key", "detail": f"Paste an API key for {preset.label}"}
        if not self._model():
            return {"ready": False, "reason": "no_model", "detail": f"Choose a {preset.label} model"}
        return {"ready": True, "reason": "", "detail": f"{preset.label} — {self._model()}"}

    def resolve_target(self, style: Mapping[str, Any], replay: Mapping[str, Any] | None) -> RenderTarget:
        preset = self._preset
        cloud = self._cloud
        model = self._model()
        # Dimensions ride the target for the same reason `model` does. An adapter
        # reading config["cloud"]["width"] at render time would hand a *rehydrate*
        # today's setting -- so an image generated at 1024x1024 comes back
        # 1536x1024 after the user touched the resolution picker, which is the exact
        # silent substitution rehydrate exists to avoid.
        width, height = int(cloud["width"]), int(cloud["height"])
        if replay:
            stored_model = replay.get("backend_model")
            if isinstance(stored_model, str) and stored_model:
                model = stored_model
            stored_w, stored_h = replay.get("width"), replay.get("height")
            if isinstance(stored_w, int) and isinstance(stored_h, int) and stored_w > 0 and stored_h > 0:
                width, height = stored_w, stored_h
        references: tuple[Mapping[str, Any], ...] = ()
        source = str(cloud.get("reference_source") or "")
        if preset is not None and preset.supports_references and preset.edits_path and source in REFERENCE_SOURCES:
            references = (
                {
                    "slot": list(CLOUD_REFERENCE_SLOT),
                    "source": source,
                    "label": "Reference image",
                    # Read per-entry by references.py: base64 in a JSON body needs a
                    # tighter cap than a multipart upload, and a provider that takes
                    # PNG/JPEG must not be handed the WebP every render is stored as.
                    "mimes": list(preset.reference_mimes),
                    "max_bytes": CLOUD_REFERENCE_MAX_BYTES,
                    # Unlike a ComfyUI graph slot, this one is optional. The same
                    # model has a plain generations endpoint one field away, so a
                    # first Visualize in a chat with no images yet renders from the
                    # prompt and says so -- refusing outright would make turning
                    # references on break every new conversation until an image
                    # exists in it.
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
        )

    # ── network ───────────────────────────────────────────────────────────────

    def _client(self, timeout: float) -> OpenAIImageClient:
        return OpenAIImageClient(
            self._base_url(),
            str(self._entry.get("api_key") or ""),
            label=self.label,
            timeout=timeout,
        )

    def _require_preset(self) -> ProviderPreset:
        preset = self._preset
        if preset is None:
            raise CloudImageError(
                f"Unknown image provider {self._cloud['provider']!r}; pick one in settings",
                "unknown_provider",
            )
        if not self._base_url():
            raise CloudImageError(f"Enter the API base URL for {preset.label}", "no_base_url")
        return preset

    async def validate_connection(self, *, allow_cached: bool = False) -> dict:
        """Model discovery **only**. This must never submit a generation.

        The shape is ComfyUI's (`{ok, capabilities, system, models}`) so
        `_test_connection` and the panel's existing rendering need no change;
        `system.devices` is simply absent here, which degrades the panel's
        "Connected — <device>" line to a bare "Connected" rather than breaking it.
        """
        preset = self._require_preset()
        client = self._client(30.0)
        models = await client.list_models(preset.models_path, preset.models_response)
        return {
            "ok": True,
            "capabilities": dict(CAPABILITIES),
            "system": {"provider": preset.label, "host": urlsplit(self._base_url()).hostname or ""},
            "models": models,
        }

    async def list_models(self) -> list[str]:
        preset = self._require_preset()
        return await self._client(30.0).list_models(preset.models_path, preset.models_response)

    async def generate(
        self,
        request: ImageRequest,
        *,
        target: RenderTarget,
        progress: ProgressCallback | None = None,
    ) -> ImageResult:
        preset = self._require_preset()
        client = self._client(request.timeout_seconds)
        # Synchronous API: one event at submit, no polling loop to report from.
        await _emit(progress, "rendering", {"backend": self.label})

        built = self._build(preset, request, target, model=target.model)
        path = preset.edits_path if request.references and preset.edits_path else preset.generations_path
        notes = [*target.notes, *built.notes]
        try:
            image = await client.create_image(path, built.body, provider_id=preset.id, timeout=request.timeout_seconds)
            model = target.model
        except CloudImageError as exc:
            configured = self._model()
            # The cloud analogue of ComfyUI's `unknown_workflow` degradation: the
            # model this image recorded is gone. Re-render on the configured one and
            # disclose it rather than refuse -- a 404 costs nothing, and a refusal
            # surfaces only as a generic 500.
            if exc.kind != MODEL_NOT_FOUND or not configured or configured == target.model:
                raise
            notes.append(f"the model this image used ({target.model}) is gone; rendered with {configured} instead")
            built = self._build(preset, request, target, model=configured)
            image = await client.create_image(path, built.body, provider_id=preset.id, timeout=request.timeout_seconds)
            notes.extend(built.notes)
            model = configured

        width, height = await asyncio.to_thread(_probe_size, image.data)
        return ImageResult(
            image_bytes=image.data,
            mime=image.mime,
            backend_info={
                "source": self.source_id,
                # ComfyUI's key names, unchanged, so existing attachments and the
                # replay path need no migration. There is no graph, so no id.
                "workflow_id": None,
                "backend_model": model,
                "provider": preset.id,
                # Real pixels, probed off the returned image rather than echoed from
                # the request: an aspect-only provider decides the actual size.
                "width": width,
                "height": height,
                "steps": None,
                "cfg": None,
                "sampler": None,
                "scheduler": None,
                # A seedless API is nondeterministic. The seed is still minted and
                # stored (rehydrate 409s on a null one), but the attachment must say
                # so rather than print a hex the render never saw.
                "seed_honored": target.supports_seed,
                "cost": image.cost,
                "references": [
                    {
                        "slot": list(reference.slot),
                        "source": reference.source,
                        "origin": reference.origin,
                        "digest": reference.digest,
                        "source_digest": reference.source_digest,
                    }
                    for reference in request.references
                ],
                "notes": notes,
            },
        )

    def _build(self, preset: ProviderPreset, request: ImageRequest, target: RenderTarget, *, model: str):
        common = {
            "model": model,
            "prompt": request.prompt,
            "negative_prompt": request.negative_prompt,
            "seed": request.seed if target.supports_seed else None,
            "width": target.width,
            "height": target.height,
            "quality": str(self._cloud.get("quality") or ""),
            # Always one. Orb stores one image per attachment, and `n` is the field
            # that silently multiplies the bill.
            "n": 1,
        }
        # Both builders are strict allowlists over the same preset, so anything the
        # provider does not declare is dropped there rather than guarded here.
        if request.references and preset.edits_path:
            return build_edit_body(preset, references=request.references, **common)
        return build_generation_body(preset, **common)


async def _emit(progress: ProgressCallback | None, stage: str, detail: Mapping[str, Any]) -> None:
    if not progress:
        return
    maybe = progress(stage, detail)
    if maybe is not None:
        await maybe


def _probe_size(data: bytes) -> tuple[int | None, int | None]:
    """The real pixel dimensions of the returned image.

    `_metadata` already writes real width/height on every ComfyUI attachment, so
    filling them here is what keeps a cloud attachment's record the same shape --
    and what lets a later rehydrate replay the size it was generated at.
    """
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as probe:
            return probe.size[0], probe.size[1]
    except Exception:
        return None, None
