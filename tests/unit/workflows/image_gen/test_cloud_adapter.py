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
from backend.workflows.image_gen.engine.providers import get_preset

XAI = get_preset("xai")
assert XAI is not None


def _png(width: int = 1024, height: int = 1024) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (30, 30, 40)).save(buf, format="PNG")
    return buf.getvalue()


# The model each provider's tests use unless one is named. Together's is a Kontext
# model because that is its only reference-capable family; NanoGPT's is its default.
_MODELS = {
    "xai": "grok-imagine-image",
    "togetherai": "black-forest-labs/FLUX.1-kontext-pro",
    "nanogpt": "cyberrealistic-xl",
    "chutes": "",
}


def _config(provider: str = "xai", model: str | None = None, **cloud) -> dict:
    """One builder for every provider under test: the rows differ by provider id and
    model, and nothing else, so three near-identical builders were three places to
    forget a field. `model=""` is a real answer -- Chutes ships no default."""
    return normalize_config(
        {
            "source": "cloud",
            "styles": [{"id": "anime", "label": "Anime"}],
            "default_style": "anime",
            "cloud": {
                "provider": provider,
                "providers": {provider: {"api_key": "sk-test", "model": _MODELS[provider] if model is None else model}},
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
async def test_a_declared_auth_probe_runs_first_and_still_never_renders():
    """NanoGPT answers its model list to a bogus key and to no key at all, so a Test
    connection resting on it reports "Connected" for a key that 401s on the first
    render the user pays to discover. The probe is a free GET, before the list."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", f"Test connection must not {request.method} {request.url.path}"
        assert "generations" not in request.url.path
        seen.append(request.url.path)
        if request.url.path.endswith("/usage"):
            return httpx.Response(200, json={"object": "usage"})
        return httpx.Response(200, json={"models": {"image": {"cyberrealistic-xl": {}, "flux-schnell": {}}}})

    result = await _adapter(_config("nanogpt"), handler).validate_connection()

    assert seen == ["/v1/usage", "/models"]
    assert result["models"] == ["cyberrealistic-xl", "flux-schnell"]


@pytest.mark.asyncio
async def test_a_rejected_key_fails_test_connection_even_though_the_list_would_pass():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/usage"):
            return httpx.Response(401, json={"error": {"message": "Incorrect API key", "code": "invalid_api_key"}})
        raise AssertionError("the model list must not be reached once the key is known bad")

    from backend.workflows.image_gen.engine.openai_image_client import CloudImageError

    with pytest.raises(CloudImageError) as excinfo:
        await _adapter(_config("nanogpt"), handler).validate_connection()
    assert excinfo.value.kind == "auth"


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
    # Overlong, so both builds emit a truncation note and the degrade has something
    # to double up on.
    result = await adapter.generate(_request(prompt="x" * (XAI.max_prompt + 50)), target=target)

    assert calls == ["grok-imagine-legacy", "grok-imagine-image"]
    assert result.backend_info["backend_model"] == "grok-imagine-image"
    notes = result.backend_info["notes"]
    assert any("grok-imagine-legacy" in note and "is gone" in note for note in notes)
    # Only the surviving attempt's build notes are kept: collecting the failed one's
    # too disclosed the same truncation twice on every degrade.
    assert sum("truncated" in note for note in notes) == 1


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
        # Chutes declares no default model, so there is genuinely nothing to run.
        ({"provider": "chutes", "providers": {"chutes": {"api_key": "k", "model": ""}}}, "no_model"),
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
    assert OpenAICompatibleImageAdapter(_config("xai", model="")).readiness()["ready"] is True


def test_readiness_judges_a_replay_on_the_model_it_recorded():
    """Clearing the model field must not refuse a rehydrate of an image whose own
    model is still there to render it -- the stored model is what will be sent."""
    adapter = OpenAICompatibleImageAdapter(_config("chutes"))
    assert adapter.readiness()["reason"] == "no_model"
    assert adapter.readiness("some/stored-model")["ready"] is True


@pytest.mark.asyncio
async def test_a_render_with_no_model_says_so_instead_of_asking_the_provider():
    """Chutes, AI/ML API, ElectronHub and `custom` all ship no
    `default_model`, so without this gate the render posts `model: ""` and the user
    reads whatever that provider makes of an empty string."""
    config = _config("chutes")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"nothing may be posted without a model, got {request.url.path}")

    from backend.workflows.image_gen.engine.openai_image_client import CloudImageError

    adapter = _adapter(config, handler)
    with pytest.raises(CloudImageError) as excinfo:
        await adapter.generate(_request(), target=_target(adapter, config))
    assert excinfo.value.kind == "no_model"
    assert "Choose a model for Chutes" in str(excinfo.value)


@pytest.mark.asyncio
async def test_test_connection_still_works_before_a_model_is_chosen():
    """The discovery gate is deliberately weaker than the render gate: listing the
    models is what fills the picker, so requiring one first makes it unreachable."""
    handler = lambda _request: httpx.Response(200, json={"data": [{"id": "some/model"}]})  # noqa: E731
    assert (await _adapter(_config("chutes"), handler).validate_connection())["models"] == ["some/model"]


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
    config = _config("togetherai", reference_source="character")
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
    config = _config("togetherai", "black-forest-labs/FLUX.1-schnell", reference_source="character")
    target = _target(OpenAICompatibleImageAdapter(config), config)

    assert target.reference_slots == ()
    assert any("does not accept reference images" in note for note in target.notes)


def test_a_reference_capable_model_is_not_nagged_about_it():
    config = _config("togetherai", reference_source="character")
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

    config = _config("togetherai", "black-forest-labs/FLUX.1-schnell", reference_source="character")
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
