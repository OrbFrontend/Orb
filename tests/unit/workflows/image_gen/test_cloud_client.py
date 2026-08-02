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
async def test_both_model_list_shapes_are_read():
    """xAI's image-model endpoint answers `{"models": [...]}`, not the OpenAI
    `{"data": [...]}`. Reading the wrong key yields an empty dropdown, which reads
    as "no models" rather than as "wrong parser"."""
    xai = await _client(_ok({"models": [{"id": "grok-imagine-image"}, {"id": "grok-imagine-image-quality"}]})).list_models(
        "/image-generation-models", "models_list"
    )
    assert xai == ["grok-imagine-image", "grok-imagine-image-quality"]

    openai = await _client(_ok({"data": [{"id": "gpt-image-1"}]})).list_models("/models", "openai_data")
    assert openai == ["gpt-image-1"]


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
    ],
    ids=["moderation", "model gone"],
)
async def test_a_refusal_carries_the_kind_the_caller_degrades_on(status, message, model, kind, expected):
    handler = lambda _request: httpx.Response(status, json={"error": {"message": message}})  # noqa: E731
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": model}, provider_id="xai", timeout=10)
    assert exc.value.kind == kind
    assert expected in str(exc.value)


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
