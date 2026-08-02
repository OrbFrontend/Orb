"""The cloud adapter: targeting, references, and the promises it must not break.

One of these guards against spending the user's money by accident (Test connection
must not render), and two against a silent substitution (a replay must pin its own
resolution and model). `n` stays 1 in `test_providers`, per preset.
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
    CLOUD_REFERENCE_MAX_BYTES,
    CLOUD_REFERENCE_SLOT,
    OpenAICompatibleImageAdapter,
)
from backend.workflows.image_gen.engine.contracts import ImageRequest, ResolvedReference
from backend.workflows.image_gen.engine.openai_image_client import OpenAIImageClient


def _png(width: int = 1024, height: int = 1024) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 30, 40)).save(buf, format="PNG")
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


def _together_config(model: str = "black-forest-labs/FLUX.1-kontext-pro", **cloud) -> dict:
    """Together has no `/images/edits` and still takes references, on the ordinary
    generations body -- the case `edits_path` gating used to make unreachable."""
    return normalize_config(
        {
            "source": "cloud",
            "styles": [{"id": "anime", "label": "Anime"}],
            "default_style": "anime",
            "cloud": {
                "provider": "togetherai",
                "providers": {"togetherai": {"api_key": "sk-test", "model": model}},
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

    # xAI declares a default model, so pasting a key alone is enough to render.
    keyed = normalize_config({"source": "cloud", "cloud": {"provider": "xai", "providers": {"xai": {"api_key": "k"}}}})
    assert OpenAICompatibleImageAdapter(keyed).readiness()["ready"] is True


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
async def test_references_ride_the_generations_body_when_there_is_no_edits_endpoint():
    """Gating on `edits_path` made Together's edit models unreachable: it has no
    `/images/edits` at all, yet FLUX.1-kontext takes an `image_url` on the ordinary
    generations call. Verified live -- the reference lands."""
    record: dict = {}
    config = _together_config(reference_source="character")
    adapter = _adapter(config, _generation_handler(record))
    request = _request(references=(_reference(_png(), "image/png"),))

    await adapter.generate(request, target=_target(adapter, config))

    assert record["path"].endswith("/images/generations")
    assert record["body"]["image_url"].startswith("data:image/png;base64,")


def test_a_model_that_cannot_take_references_declares_no_slot_and_says_so():
    """No slot is what stops the reference reaching a model that cannot use it --
    and the provider's answer is not uniform enough to rely on: FLUX.2 rejects
    `image_url`, schnell returns 200 having ignored it. The note is the difference
    between that and the reference quietly going missing."""
    config = _together_config("black-forest-labs/FLUX.1-schnell", reference_source="character")
    target = _target(OpenAICompatibleImageAdapter(config), config)

    assert target.reference_slots == ()
    assert any("does not accept reference images" in note for note in target.notes)


def test_a_reference_capable_model_is_not_nagged_about_it():
    config = _together_config(reference_source="character")
    target = _target(OpenAICompatibleImageAdapter(config), config)

    assert len(target.reference_slots) == 1
    assert target.notes == ()


@pytest.mark.asyncio
async def test_a_gone_model_degrades_onto_one_that_cannot_take_the_reference():
    """The substitute is not the model the slot was offered for. Replaying a Kontext
    image after the connection moved to a text-to-image model must drop the
    reference, not re-send `image_url` to something that answers 400 -- that would
    turn a graceful degrade into a hard failure."""
    record: dict = {}
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(404, json={"error": {"message": "no such model"}})
        record["path"] = request.url.path
        record["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})

    config = _together_config("black-forest-labs/FLUX.1-schnell", reference_source="character")
    adapter = _adapter(config, handler)
    target = _target(adapter, config, replay={"backend_model": "black-forest-labs/FLUX.1-kontext-pro"})
    assert len(target.reference_slots) == 1

    result = await adapter.generate(_request(references=(_reference(_png(), "image/png"),)), target=target)

    assert "image_url" not in record["body"]
    notes = result.backend_info["notes"]
    assert any("does not accept reference images" in note for note in notes)
    assert any("is gone" in note for note in notes)


def test_the_reference_slot_declares_the_policy_that_bounds_it():
    """The slot's own record is what `references.py` reads: the provider's accepted
    mimes and the tighter base64-in-JSON cap. `test_display_encode` owns what those
    two then do to the bytes."""
    config = _config(reference_source="character")
    (slot,) = _target(OpenAICompatibleImageAdapter(config), config).reference_slots

    assert tuple(slot["mimes"]) == ("image/png", "image/jpeg")
    assert slot["max_bytes"] == CLOUD_REFERENCE_MAX_BYTES
    # Optional, unlike a ComfyUI graph slot: the same model has a plain generations
    # endpoint one field away, so an imageless chat renders from the prompt.
    assert slot["required"] is False
