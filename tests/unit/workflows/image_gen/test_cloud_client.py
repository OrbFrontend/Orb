"""The cloud HTTP client: both decode paths, the byte cap, and the error funnel.

The funnel is the reason this file is long. A provider 400 is genuinely
actionable ("Argument not supported: size"), so unlike ComfyUI's totally opaque
message this one echoes an excerpt -- which makes "what must never survive the
scrub" a property worth pinning, not a comment.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from backend.workflows.image_gen.engine.image_bytes import MAX_IMAGE_BYTES
from backend.workflows.image_gen.engine.openai_image_client import (
    MODEL_NOT_FOUND,
    CloudImageError,
    OpenAIImageClient,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels" * 32
KEY = "sk-live-do-not-leak"


def _client(handler, **kwargs) -> OpenAIImageClient:
    return OpenAIImageClient(
        "https://api.example.test/v1",
        KEY,
        label="xAI (Grok)",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _ok(payload):
    return lambda _request: httpx.Response(200, json=payload)


# ── decoding ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_b64_json_is_decoded_without_a_second_request():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

    image = await _client(handler).create_image(
        "/images/generations", {"model": "m", "prompt": "p"}, provider_id="xai", timeout=10
    )

    assert seen == ["/v1/images/generations"]
    assert image.data
    # Stored as WebP like every other render, so the chat inlines one format.
    assert image.mime in ("image/webp", "image/png")


@pytest.mark.asyncio
async def test_a_hosted_url_is_fetched_and_the_cost_unit_is_carried_verbatim():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example.test/out.png"}], "usage": {"cost_in_usd_ticks": 1400}},
            )
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    image = await _client(handler).create_image(
        "/images/generations", {"model": "m", "prompt": "p"}, provider_id="xai", timeout=10
    )

    # Never renamed to a currency: nothing documents what a tick is worth, and
    # picking a divisor by omission prints a wrong number on a billing figure.
    assert image.cost == {"provider": "xai", "unit": "usd_ticks", "value": 1400}


@pytest.mark.asyncio
async def test_a_plaintext_result_url_is_refused():
    handler = _ok({"data": [{"url": "http://cdn.example.test/out.png"}]})
    with pytest.raises(CloudImageError, match="insecure"):
        await _client(handler).create_image("/images/generations", {}, provider_id="xai", timeout=10)


@pytest.mark.asyncio
async def test_an_oversized_download_is_refused_while_streaming():
    """The cap is enforced against bytes actually read, not against `content-length`
    -- a server is free to understate that header, and by the time you notice you
    have already buffered the payload."""
    oversized = PNG + b"\x00" * (MAX_IMAGE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example.test/out.png"}]})
        return httpx.Response(200, content=oversized, headers={"content-length": "12"})

    with pytest.raises(CloudImageError, match="too large"):
        await _client(handler).create_image("/images/generations", {}, provider_id="xai", timeout=10)


@pytest.mark.asyncio
async def test_a_non_image_payload_is_refused():
    handler = _ok({"data": [{"b64_json": base64.b64encode(b"<html>nope</html>").decode()}]})
    with pytest.raises(CloudImageError, match="not a supported image"):
        await _client(handler).create_image("/images/generations", {}, provider_id="xai", timeout=10)


# ── model listing ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_three_model_list_shapes_are_read():
    """xAI's image-model endpoint answers `{"models": [...]}`, OpenAI answers
    `{"data": [...]}`, and Together answers a bare array. Reading the wrong shape
    yields an empty dropdown, which reads as "no models" rather than as "wrong
    parser" -- Together's row shipped as `openai_data` and failed Test connection
    against a provider that was answering perfectly."""
    xai = await _client(_ok({"models": [{"id": "grok-imagine-image"}, {"id": "grok-imagine-image-quality"}]})).list_models(
        "/image-generation-models", "models_list"
    )
    assert xai == ["grok-imagine-image", "grok-imagine-image-quality"]

    openai = await _client(_ok({"data": [{"id": "gpt-image-1"}]})).list_models("/models", "openai_data")
    assert openai == ["gpt-image-1"]

    together = await _client(_ok([{"id": "flux", "type": "image"}, {"id": "kimi", "type": "chat"}])).list_models(
        "/models", "bare_list"
    )
    assert together == ["flux", "kimi"]


@pytest.mark.asyncio
async def test_the_nanogpt_shape_reads_ids_off_a_mapping_not_a_list():
    """NanoGPT's image catalogue is `{"models": {"image": {id: {...}}}}` -- the ids
    are the keys, and the values are per-model capability records Orb does not read.
    Its documented `/v1/models` is a perfectly ordinary `{"data": [...]}` holding 653
    models, none of which make an image, so a row that reads the ordinary shape gets
    a full dropdown and no way to render."""
    payload = {
        "models": {
            "text": {"claude-opus-5": {"name": "Claude"}},
            "image": {"cyberrealistic-xl": {"iconLabel": "both"}, "flux-schnell": {"iconLabel": "text-to-image"}},
            "video": {"veo": {}},
        }
    }
    models = await _client(_ok(payload)).list_models("../models", "nanogpt_image_map")
    assert models == ["cyberrealistic-xl", "flux-schnell"]


@pytest.mark.asyncio
async def test_the_nanogpt_shape_pointed_at_a_list_is_malformed():
    """The same guard the other shapes get: a silently-empty picker reads to the
    user as "this key has no models", not as "Orb parsed the wrong thing"."""
    with pytest.raises(CloudImageError) as excinfo:
        await _client(_ok({"data": [{"id": "flux"}]})).list_models("../models", "nanogpt_image_map")
    assert excinfo.value.kind == "malformed"


@pytest.mark.asyncio
async def test_a_relative_models_path_climbs_off_the_version_prefix():
    """NanoGPT serves `/api/v1/images/*` but `/api/models`, so the preset reaches it
    with `../models` rather than the base URL being wrong for everything else."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"models": {"image": {"cyberrealistic-xl": {}}}})

    await _client(handler).list_models("../models", "nanogpt_image_map")
    assert seen == ["/models"]


@pytest.mark.asyncio
async def test_verify_key_raises_auth_on_a_401_and_is_silent_otherwise():
    """The endpoint exists because NanoGPT serves its model list to anonymous
    callers: resting Test connection on that list reports "Connected" for a key that
    401s on the first render the user pays to discover."""
    seen: list[str] = []

    def good(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"object": "usage"})

    assert await _client(good).verify_key("/usage") is None
    assert seen == ["/v1/usage"]

    rejected = lambda _request: httpx.Response(  # noqa: E731
        401, json={"error": {"message": "Incorrect API key", "code": "invalid_api_key"}}
    )
    with pytest.raises(CloudImageError) as excinfo:
        await _client(rejected).verify_key("/usage")
    assert excinfo.value.kind == "auth"


@pytest.mark.asyncio
async def test_a_type_filter_keeps_only_that_modality():
    """Together hosts one catalogue for everything: 271 entries, 29 of them image
    models. Unfiltered, the picker is mostly chat models that cannot render."""
    entries = [
        {"id": "flux-schnell", "type": "image"},
        {"id": "kimi-k3", "type": "chat"},
        {"id": "whisper", "type": "audio"},
        {"id": "seedream", "type": "image"},
    ]
    models = await _client(_ok(entries)).list_models("/models", "bare_list", "image")
    assert models == ["flux-schnell", "seedream"]


@pytest.mark.asyncio
async def test_an_untagged_catalogue_falls_back_to_the_whole_list():
    """The filter is an optimisation, never a gate. A provider that stops tagging
    its entries should cost a longer list, not an empty picker that reads to the
    user as "this key has no models"."""
    models = await _client(_ok([{"id": "flux"}, {"id": "sdxl"}])).list_models("/models", "bare_list", "image")
    assert models == ["flux", "sdxl"]


@pytest.mark.asyncio
async def test_a_bare_list_shape_pointed_at_a_mapping_is_still_malformed():
    with pytest.raises(CloudImageError) as excinfo:
        await _client(_ok({"data": [{"id": "flux"}]})).list_models("/models", "bare_list")
    assert excinfo.value.kind == "malformed"


# ── the error funnel ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, payload, expected",
    [
        (401, {"error": {"message": "bad key"}}, "The API key for xAI (Grok) was rejected"),
        (403, {"error": "forbidden"}, "The API key for xAI (Grok) was rejected"),
        (429, {"error": {"message": "slow down"}}, "rate-limiting"),
        (500, {"error": {"message": "boom"}}, "Could not communicate with xAI (Grok)"),
    ],
)
async def test_each_status_gets_its_own_message(status, payload, expected):
    handler = lambda _request: httpx.Response(status, json=payload)  # noqa: E731
    with pytest.raises(CloudImageError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message", "model", "kind", "expected"),
    [
        # For roleplay imagery on a commercial API a refusal is the dominant failure
        # mode, and the whole difference between "Orb is broken" and "it said no".
        (400, "Your request was rejected by our content policy", "m", "moderation", "content policy"),
        # Flagged so the adapter can re-render on the configured model and disclose.
        (404, "no such model", "grok-imagine-legacy", MODEL_NOT_FOUND, "grok-imagine-legacy"),
        # The providers do not agree on the status: NanoGPT answers 400 for a model
        # it does not have, so a branch gated on 404 alone leaves the degrade path
        # dead there and a rehydrate of a retired model dying as a bare rejection.
        (400, "Invalid image model specified.", "retired-model", MODEL_NOT_FOUND, "retired-model"),
    ],
    ids=["moderation", "model gone", "model gone on a 400"],
)
async def test_a_refusal_carries_the_kind_the_caller_degrades_on(status, message, model, kind, expected):
    handler = lambda _request: httpx.Response(status, json={"error": {"message": message}})  # noqa: E731
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": model}, provider_id="xai", timeout=10)
    assert exc.value.kind == kind
    assert expected in str(exc.value)


@pytest.mark.asyncio
async def test_an_unknown_model_is_recognised_from_the_code_when_the_message_is_prose():
    """NanoGPT puts `invalid_model` in `error.code` and again at the top level. The
    branch reads the codes rather than resting on the human sentence, which a
    provider is free to reword between releases."""
    handler = lambda _request: httpx.Response(  # noqa: E731
        400,
        json={
            "error": {"message": "We could not run that.", "type": "invalid_request_error", "code": "invalid_model"},
            "code": "invalid_model",
        },
    )
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "gone"}, provider_id="nanogpt", timeout=10)
    assert exc.value.kind == MODEL_NOT_FOUND


@pytest.mark.asyncio
async def test_a_top_level_cost_is_read_when_there_is_no_usage_block():
    """NanoGPT reports `{"cost": 0.0034}` beside `data`, in plain USD, and nothing
    under `usage`. Reading `usage` alone showed no cost row on a render that had
    one -- which reads as free rather than as unreported."""
    payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}], "cost": 0.0034, "remainingBalance": "4.77"}
    image = await _client(_ok(payload)).create_image("/images/generations", {"model": "m"}, provider_id="nanogpt", timeout=10)
    assert image.cost == {"provider": "nanogpt", "unit": "usd", "value": 0.0034}


@pytest.mark.asyncio
async def test_a_usage_block_still_wins_over_a_top_level_cost():
    """A provider reporting both means the nested one; the top level is the fallback
    for providers that have no `usage` at all."""
    payload = {
        "data": [{"b64_json": base64.b64encode(PNG).decode()}],
        "usage": {"cost_in_usd_ticks": 1400},
        "cost": 99,
    }
    image = await _client(_ok(payload)).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert image.cost == {"provider": "xai", "unit": "usd_ticks", "value": 1400}


@pytest.mark.asyncio
async def test_an_actionable_400_is_echoed_but_never_carries_a_path_or_the_key():
    """Mirrors `test_validation_error_is_sanitized_and_names_checkpoint`: the useful
    half of a provider 400 reaches the user, and nothing about the server's
    internals -- or the credential -- rides along with it."""
    handler = lambda _request: httpx.Response(  # noqa: E731
        400,
        json={
            "error": {
                "message": (
                    "Argument not supported: size at /var/run/grok/handlers/images.py:214 "
                    f"(see https://api.example.test/internal/trace?key={KEY})"
                )
            }
        },
    )
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)

    message = str(exc.value)
    assert "Argument not supported: size" in message
    assert "/var/run" not in message
    assert "images.py" not in message
    assert "https://" not in message
    assert KEY not in message


@pytest.mark.asyncio
async def test_a_transport_failure_never_reveals_the_endpoint():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to https://api.example.test/v1")

    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert str(exc.value) == "Could not communicate with xAI (Grok)"
