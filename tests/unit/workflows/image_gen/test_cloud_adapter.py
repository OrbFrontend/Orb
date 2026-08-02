"""The cloud adapter: targeting, references, and the promises it must not break.

Two of these are guards against spending the user's money by accident (Test
connection must not render; `n` must stay 1), and two are guards against a silent
substitution (a replay must pin its own resolution and model).
"""

from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.engine.adapters.openai_image import (
    CLOUD_REFERENCE_SLOT,
    OpenAICompatibleImageAdapter,
)
from backend.workflows.image_gen.engine.contracts import ImageRequest, ResolvedReference
from backend.workflows.image_gen.engine.display_encode import shrink_for_display
from backend.workflows.image_gen.engine.openai_image_client import OpenAIImageClient


def _png(width: int = 1024, height: int = 1024) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 30, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (512, 512), (90, 20, 20)).save(buf, format="WEBP")
    return buf.getvalue()


def _photo(width: int, height: int, *, fmt: str = "PNG") -> bytes:
    """Noise, not a flat fill: a flat image compresses to nothing and would prove
    nothing about a size cap or about which target format is smaller."""
    buf = io.BytesIO()
    Image.effect_noise((width, height), 64).convert("RGB").save(buf, format=fmt)
    return buf.getvalue()


def _config(**cloud) -> dict:
    return normalize_config(
        {
            "source": "cloud",
            "styles": [{"id": "anime", "label": "Anime"}],
            "default_style": "anime",
            "cloud": {
                "provider": "xai",
                "providers": {"xai": {"api_key": "sk-test", "model": "grok-imagine-image"}},
                **cloud,
            },
        }
    )


def _adapter(config, handler) -> OpenAICompatibleImageAdapter:
    """The adapter with its one network seam swapped for a MockTransport, exactly
    as `test_external_adapter` swaps `ComfyClient`."""

    class _Mocked(OpenAICompatibleImageAdapter):
        def _client(self, timeout: float) -> OpenAIImageClient:
            return OpenAIImageClient(
                "https://api.x.ai/v1",
                "sk-test",
                label=self.label,
                transport=httpx.MockTransport(handler),
            )

    return _Mocked(config)


def _target(adapter, config, replay=None):
    return adapter.resolve_target(resolve_style(config, "anime"), replay)


def _request(**kwargs) -> ImageRequest:
    return ImageRequest(
        prompt=kwargs.pop("prompt", "a quiet room"),
        negative_prompt=kwargs.pop("negative_prompt", "blurry"),
        seed=kwargs.pop("seed", 12345),
        style_id="anime",
        timeout_seconds=10,
        **kwargs,
    )


def _generation_handler(record: dict, *, image: bytes | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        record["path"] = request.url.path
        record["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(image or _png()).decode()}],
                "usage": {"cost_in_usd_ticks": 900},
            },
        )

    return handler


# ── the money guards ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_connection_never_posts_to_the_generations_path():
    """A Test-connection button that bills the user is unacceptable. The handler
    fails the test rather than the assertion doing it afterwards, so a POST cannot
    slip through by being made and then ignored."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", f"Test connection must not {request.method} {request.url.path}"
        assert "generations" not in request.url.path
        return httpx.Response(200, json={"models": [{"id": "grok-imagine-image"}]})

    config = _config()
    result = await _adapter(config, handler).validate_connection()

    assert result["ok"] is True
    assert result["models"] == ["grok-imagine-image"]
    # ComfyUI's shape, so `_test_connection` and the panel need no change. `devices`
    # is simply absent, which degrades "Connected — <device>" to "Connected".
    assert set(result) == {"ok", "capabilities", "system", "models"}
    assert result["system"] == {"provider": "xAI (Grok)", "host": "api.x.ai"}
    assert "devices" not in result["system"]


@pytest.mark.asyncio
async def test_n_is_always_one():
    record: dict = {}
    config = _config()
    adapter = _adapter(config, _generation_handler(record))
    await adapter.generate(_request(), target=_target(adapter, config))
    assert record["body"]["n"] == 1


# ── targeting ────────────────────────────────────────────────────────────────


def test_a_fresh_target_reads_the_configured_model_and_resolution():
    config = _config(width=1536, height=1024)
    target = _target(OpenAICompatibleImageAdapter(config), config)
    assert (target.source, target.target_id, target.model) == ("cloud", "", "grok-imagine-image")
    assert (target.width, target.height) == (1536, 1024)
    # xAI honours neither, so the composer is told not to write an `avoid` and the
    # attachment will say the seed was unused.
    assert target.supports_negative_prompt is False
    assert target.supports_seed is False


def test_a_replay_pins_the_resolution_it_was_generated_at_not_todays():
    """The exact silent substitution rehydrate exists to avoid: an image made at
    1024x1024 must not come back 1536x1024 because the picker moved since."""
    config = _config(width=1536, height=1024)
    target = _target(
        OpenAICompatibleImageAdapter(config),
        config,
        {"backend_model": "grok-imagine-image-quality", "width": 1024, "height": 1024},
    )
    assert (target.width, target.height) == (1024, 1024)
    assert target.model == "grok-imagine-image-quality"


@pytest.mark.asyncio
async def test_a_recorded_model_that_is_gone_degrades_with_disclosure():
    """The cloud analogue of ComfyUI's `unknown_workflow` degradation. A 404 costs
    nothing, and refusing surfaces only as a generic 500."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "grok-imagine-legacy":
            return httpx.Response(404, json={"error": {"message": "no such model"}})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})

    config = _config()
    adapter = _adapter(config, handler)
    target = _target(adapter, config, {"backend_model": "grok-imagine-legacy"})
    result = await adapter.generate(_request(), target=target)

    assert calls == ["grok-imagine-legacy", "grok-imagine-image"]
    assert result.backend_info["backend_model"] == "grok-imagine-image"
    assert any("grok-imagine-legacy" in note and "is gone" in note for note in result.backend_info["notes"])


@pytest.mark.asyncio
async def test_the_attachment_records_real_pixels_and_an_unhonoured_seed():
    record: dict = {}
    config = _config()
    adapter = _adapter(config, _generation_handler(record, image=_png(1024, 768)))
    result = await adapter.generate(_request(), target=_target(adapter, config))

    assert (result.backend_info["width"], result.backend_info["height"]) == (1024, 768)
    # Probed off the returned image, not echoed from the request: an aspect-only
    # provider decides the actual size.
    assert result.backend_info["seed_honored"] is False
    assert result.backend_info["cost"] == {"provider": "xai", "unit": "usd_ticks", "value": 900}
    assert result.backend_info["steps"] is None


# ── readiness ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cloud, reason",
    [
        ({"provider": "not_a_provider", "providers": {"not_a_provider": {"api_key": "k", "model": "m"}}}, "unknown_provider"),
        ({"provider": "xai", "providers": {"xai": {"api_key": "", "model": "m"}}}, "no_api_key"),
        # OpenRouter declares no default model, so there is genuinely nothing to run.
        ({"provider": "openrouter", "providers": {"openrouter": {"api_key": "k", "model": ""}}}, "no_model"),
        ({"provider": "custom", "providers": {"custom": {"api_key": "k", "model": "m"}}}, "no_base_url"),
    ],
)
def test_readiness_names_the_gap(cloud, reason):
    config = normalize_config({"source": "cloud", "cloud": cloud})
    answer = OpenAICompatibleImageAdapter(config).readiness()
    assert answer["reason"] == reason
    assert answer["ready"] is False
    assert answer["detail"]


def test_a_configured_provider_is_ready():
    answer = OpenAICompatibleImageAdapter(_config()).readiness()
    assert answer["ready"] is True
    assert "grok-imagine-image" in answer["detail"]


def test_an_unconfigured_model_falls_back_to_the_presets_default():
    """xAI declares a default, so pasting a key is enough to render."""
    config = normalize_config({"source": "cloud", "cloud": {"provider": "xai", "providers": {"xai": {"api_key": "k"}}}})
    assert OpenAICompatibleImageAdapter(config).readiness()["ready"] is True


# ── references ───────────────────────────────────────────────────────────────


def _reference(data: bytes, mime: str) -> ResolvedReference:
    return ResolvedReference(
        slot=CLOUD_REFERENCE_SLOT,
        source="character",
        data=data,
        mime=mime,
        origin="character:card-1",
        digest="d" * 64,
    )


def test_reference_slots_appear_only_when_the_source_is_turned_on():
    """Sending conversation images to a third party is opt-in, so "" is off."""
    off = _target(OpenAICompatibleImageAdapter(_config()), _config())
    assert off.reference_slots == ()

    config = _config(reference_source="previous_or_character")
    on = _target(OpenAICompatibleImageAdapter(config), config)
    assert len(on.reference_slots) == 1
    assert on.reference_slots[0]["slot"] == list(CLOUD_REFERENCE_SLOT)
    assert on.reference_slots[0]["source"] == "previous_or_character"


@pytest.mark.asyncio
async def test_references_route_to_the_edits_path_as_data_uris():
    record: dict = {}
    config = _config(reference_source="character")
    adapter = _adapter(config, _generation_handler(record))
    request = _request(references=(_reference(_png(), "image/png"),))

    await adapter.generate(request, target=_target(adapter, config))

    assert record["path"].endswith("/images/edits")
    assert record["body"]["images"][0]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_no_references_means_the_generations_path():
    record: dict = {}
    config = _config(reference_source="character")
    adapter = _adapter(config, _generation_handler(record))
    await adapter.generate(_request(), target=_target(adapter, config))
    assert record["path"].endswith("/images/generations")


@pytest.mark.asyncio
async def test_a_stored_webp_reference_arrives_as_a_mime_the_provider_accepts():
    """The common path, not a corner: every render is stored as WebP, so a reference
    resolving to `previous` is WebP and comfortably under both size ceilings. A
    size-only gate would let it sail through into a JSON body that told the
    provider PNG/JPEG."""
    from backend.workflows.image_gen.engine.display_encode import normalize_reference

    preset_mimes = ("image/png", "image/jpeg")
    data, mime = normalize_reference(_webp(), "image/webp", allowed=preset_mimes, max_bytes=4 * 1024 * 1024)

    assert mime != "image/webp"
    assert mime in preset_mimes
    # And the bytes really are that format, not just relabelled.
    with Image.open(io.BytesIO(data)) as probe:
        assert probe.format == "JPEG"


def test_the_conversion_target_is_the_lossy_one_the_provider_allows():
    """PNG is lossless and a reference is photographic, so preferring it tripled
    the bytes of the WebP every render is already stored as -- on the common path,
    not an edge case. The preference is by compression, not by preset order."""
    from backend.workflows.image_gen.engine.display_encode import normalize_reference

    source = _photo(1024, 1536)
    stored, stored_mime = shrink_for_display(source, "image/png")
    as_png, _ = normalize_reference(stored, stored_mime, allowed=("image/png",), max_bytes=4 * 1024 * 1024)
    as_either, mime = normalize_reference(stored, stored_mime, allowed=("image/png", "image/jpeg"), max_bytes=4 * 1024 * 1024)

    assert mime == "image/jpeg"
    assert len(as_either) < len(as_png)


@pytest.mark.parametrize(
    ("data", "mime"),
    [
        # Bigger than the cap in a mime the provider already accepts: the old
        # "keep whichever is smaller" rule handed this straight back, oversized.
        (_photo(4000, 3000, fmt="JPEG"), "image/jpeg"),
        # And the same picture arriving as the WebP a render is stored as.
        (_photo(4000, 3000, fmt="WEBP"), "image/webp"),
    ],
    ids=["already an accepted mime", "needs conversion too"],
)
def test_an_oversized_cloud_reference_is_brought_under_the_declared_cap(data, mime):
    """`max_bytes` is a contract, not a hint: the reference rides base64 inside a
    JSON body, so an unenforced cap is a multi-megabyte POST the provider rejects."""
    from backend.workflows.image_gen.engine.display_encode import normalize_reference

    cap = 1024 * 1024
    assert len(data) > cap
    out, out_mime = normalize_reference(data, mime, allowed=("image/png", "image/jpeg"), max_bytes=cap)

    assert len(out) <= cap
    assert out_mime in ("image/png", "image/jpeg")


def test_a_reference_orb_cannot_decode_is_refused_rather_than_mislabelled():
    """`force`'s whole job is "the destination cannot read the input format". The
    decode-failure path used to return the input unchanged, so an SVG or a HEIC
    upload reached the provider inside a body that declared PNG."""
    from backend.workflows.image_gen.engine.contracts import ImageGenerationError
    from backend.workflows.image_gen.engine.display_encode import normalize_reference

    with pytest.raises(ImageGenerationError, match="could not read"):
        normalize_reference(b"<svg/>", "image/svg+xml", allowed=("image/png", "image/jpeg"), max_bytes=4 * 1024 * 1024)

    # With nothing declared there is no contract to break, so the old promise
    # holds: a reference Orb cannot read is still one ComfyUI probably can.
    assert normalize_reference(b"<svg/>", "image/svg+xml") == (b"<svg/>", "image/svg+xml")
