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
@pytest.mark.parametrize(
    "shape, payload, expected",
    [
        (
            "models_list",
            {"models": [{"id": "grok-imagine-image"}, {"id": "grok-imagine-image-quality"}]},
            ["grok-imagine-image", "grok-imagine-image-quality"],
        ),
        ("openai_data", {"data": [{"id": "gpt-image-1"}]}, ["gpt-image-1"]),
        ("bare_list", [{"id": "flux", "type": "image"}, {"id": "kimi", "type": "chat"}], ["flux", "kimi"]),
        # The ids are the *keys*, and the values are per-model capability records Orb
        # does not read. NanoGPT's documented `/v1/models` is an ordinary
        # `{"data": [...]}` of 653 models, none of which make an image, so a row that
        # reads the ordinary shape gets a full dropdown and no way to render.
        (
            "nanogpt_image_map",
            {
                "models": {
                    "text": {"claude-opus-5": {"name": "Claude"}},
                    "image": {"cyberrealistic-xl": {"iconLabel": "both"}, "flux-schnell": {}},
                    "video": {"veo": {}},
                }
            },
            ["cyberrealistic-xl", "flux-schnell"],
        ),
    ],
    ids=["xai", "openai", "together", "nanogpt"],
)
async def test_every_declared_model_list_shape_is_read(shape, payload, expected):
    """Reading the wrong shape yields an empty dropdown, which reads as "no models"
    rather than as "wrong parser" -- Together's row shipped as `openai_data` and
    failed Test connection against a provider that was answering perfectly."""
    assert await _client(_ok(payload)).list_models("/models", shape) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    ["nanogpt_image_map", "bare_list"],
    ids=["a map shape pointed at a list", "a list shape pointed at a map"],
)
async def test_a_shape_mismatch_is_malformed_rather_than_empty(shape):
    """A silently-empty picker reads to the user as "this key has no models", not as
    "Orb parsed the wrong thing"."""
    with pytest.raises(CloudImageError) as excinfo:
        await _client(_ok({"data": [{"id": "flux"}]})).list_models("/models", shape)
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
    models = await _client(_ok(entries)).list_models("/models", "bare_list", "type_image")
    assert models == ["flux-schnell", "seedream"]


@pytest.mark.asyncio
async def test_a_modality_filter_reads_outputs_and_never_inputs():
    """OpenRouter's 337-entry catalogue has no `type` field at all, and matching on
    `input_modalities` would keep every text model that can *read* a picture -- which
    is most of them, and none of which renders one."""
    entries = [
        {
            "id": "google/gemini-2.5-flash-image",
            "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["image", "text"]},
        },
        {
            "id": "anthropic/claude-opus-5",
            "architecture": {"input_modalities": ["image", "text"], "output_modalities": ["text"]},
        },
        {"id": "deepseek/deepseek-v4-flash", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
        # The shape is declared, never assumed: a row missing `architecture`
        # entirely must be skipped rather than raise on the whole catalogue.
        {"id": "mystery-model"},
    ]
    models = await _client(_ok({"data": entries})).list_models("/models", "openai_data", "output_image")
    assert models == ["google/gemini-2.5-flash-image"]


@pytest.mark.asyncio
async def test_an_id_filter_is_the_only_rule_openai_s_catalogue_supports():
    """OpenAI's 125 entries carry `id`, `object`, `created` and `owned_by` -- no
    `type` like Together, no `architecture` like OpenRouter. Nothing in the payload
    answers "does this make images", so the id is read, and the images path answers
    *"The model 'gpt-4o' does not exist"* for everything this drops.
    """
    # Trimmed to the ids that carry a decision. `chatgpt-image-latest` is why the rule
    # matches "image" anywhere rather than a `gpt-image` prefix; `dall-e-3` stays in
    # the vocabulary though OpenAI no longer lists one.
    entries = [{"id": name} for name in ("gpt-image-1", "chatgpt-image-latest", "dall-e-3", "gpt-4o", "whisper-1")]
    models = await _client(_ok({"data": entries})).list_models("/models", "openai_data", "openai_image_ids")
    assert models == ["chatgpt-image-latest", "dall-e-3", "gpt-image-1"]


@pytest.mark.asyncio
async def test_an_untagged_catalogue_falls_back_to_the_whole_list():
    """The filter is an optimisation, never a gate. A provider that stops tagging
    its entries should cost a longer list, not an empty picker that reads to the
    user as "this key has no models"."""
    models = await _client(_ok([{"id": "flux"}, {"id": "sdxl"}])).list_models("/models", "bare_list", "type_image")
    assert models == ["flux", "sdxl"]


# ── the error funnel ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, payload, expected",
    [
        (401, {"error": {"message": "bad key"}}, "The API key for xAI (Grok) was rejected (HTTP 401): bad key"),
        # 403 is the loose status -- providers spend it on content refusals and plan
        # restrictions too -- so the excerpt is what keeps Orb's guess from hiding
        # the reason when the guess is wrong.
        (403, {"error": "forbidden"}, "The API key for xAI (Grok) was rejected (HTTP 403): forbidden"),
        # No bespoke sentence for 429: it is not always a per-minute rate limit, and
        # "try again in a moment" was the wrong advice for a key out of quota.
        (429, {"error": {"message": "daily quota exhausted"}}, "rejected the request (HTTP 429): daily quota exhausted"),
        # Not "could not communicate": the exchange succeeded, and the provider
        # explained its own failure.
        (500, {"error": {"message": "upstream overloaded"}}, "failed to render this request (HTTP 500): upstream overloaded"),
    ],
    ids=["401", "403", "429 is not advice", "5xx is not a network failure"],
)
async def test_every_status_carries_the_code_and_what_the_provider_said(status, payload, expected):
    handler = lambda _request: httpx.Response(status, json=payload)  # noqa: E731
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert expected in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected",
    [
        # OpenRouter buries its reason under `error.metadata`; nothing FastAPI-shaped
        # puts a string in `detail` at all. Both used to reach the user as a bare
        # "rejected the request" with nothing after it.
        ({"error": {"metadata": {"raw": "flagged upstream"}, "code": 403}}, "flagged upstream"),
        ({"detail": [{"loc": ["body", "size"], "msg": "unexpected value"}]}, "unexpected value"),
        ({"errors": [{"title": "quota exceeded"}]}, "quota exceeded"),
    ],
    ids=["nested under metadata", "a detail list", "a shape nobody enumerated"],
)
async def test_an_unenumerated_body_shape_still_says_something(payload, expected):
    """The generic walk, which is why the well-known keys can stay a short list
    instead of growing a row per provider."""
    handler = lambda _request: httpx.Response(400, json=payload)  # noqa: E731
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert expected in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        # The `_MODERATION_MARKERS` list this replaced matched "not allowed" and
        # "blocked", so both of these were reported as content refusals -- telling the
        # user to reword a prompt that was never the problem, and dropping the
        # sentence that named the real one.
        "Model gpt-image-1 is not allowed for your account tier",
        "Request blocked: negative_prompt not allowed for this model",
    ],
    ids=["a plan restriction", "an unsupported parameter"],
)
async def test_ordinary_api_english_is_not_read_as_a_content_refusal(message):
    handler = lambda _request: httpx.Response(400, json={"error": {"message": message}})  # noqa: E731
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert message in str(exc.value)
    assert "content policy" not in str(exc.value)


@pytest.mark.asyncio
async def test_a_content_refusal_still_reaches_the_user_in_the_provider_s_own_words():
    """The case the deleted branch existed for. For roleplay imagery on a commercial
    API a refusal is the dominant failure mode -- it just does not need Orb to
    recognise it, because the provider already said so."""
    handler = lambda _request: httpx.Response(  # noqa: E731
        400, json={"error": {"message": "Your request was rejected by our content policy"}}
    )
    with pytest.raises(CloudImageError, match="content policy"):
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)


@pytest.mark.asyncio
async def test_a_refusal_keeps_the_reason_it_puts_last():
    """OpenAI's live refusal body, verbatim. It is 203 characters and spends the
    first 175 on a support address and a request id, so the category -- the only part
    a user can act on -- is last. A 200-character cap ended it at
    "safety_violations=[sexua", cutting the one informative token in the sentence.

    Roleplay imagery is the dominant failure mode on a commercial API, so this is the
    message users read most often.
    """
    message = (
        "Your request was rejected by the safety system. If you believe this is an error, contact us at "
        "help.openai.com and include the request ID req_f70cba2c7faf4c96a24e7fad962594ce. "
        "safety_violations=[sexual]."
    )
    handler = lambda _request: httpx.Response(  # noqa: E731
        400, json={"error": {"message": message, "code": "moderation_blocked"}}
    )
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="openai", timeout=10)
    assert "safety_violations=[sexual]" in str(exc.value)
    # Still capped, and still not classified: `moderation_blocked` is the provider's
    # word for it and the funnel branches on nothing here.
    assert exc.value.kind == "request"


@pytest.mark.asyncio
async def test_a_timeout_is_not_reported_as_a_broken_network():
    """On a render this is the *expected* failure, and "could not communicate" sent
    the user to look at their connection for a provider that was merely slow."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert exc.value.kind == "timeout"
    assert "did not respond within 10s" in str(exc.value)


@pytest.mark.asyncio
async def test_a_200_that_is_not_json_is_named_as_such():
    """A proxy or captive portal answering in the provider's place. Reported as
    "could not communicate" it sent the user to their network for a request that
    completed."""
    handler = lambda _request: httpx.Response(200, text="<html>Sign in to continue</html>")  # noqa: E731
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)
    assert exc.value.kind == "malformed"
    assert "malformed response (HTTP 200)" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message", "model", "kind", "expected"),
    [
        # Flagged so the adapter can re-render on the configured model and disclose.
        (404, "no such model", "grok-imagine-legacy", MODEL_NOT_FOUND, "grok-imagine-legacy"),
        # The providers do not agree on the status: NanoGPT answers 400 for a model
        # it does not have, so a branch gated on 404 alone leaves the degrade path
        # dead there and a rehydrate of a retired model dying as a bare rejection.
        (400, "Invalid image model specified.", "retired-model", MODEL_NOT_FOUND, "retired-model"),
        # OpenAI: also a 400, and its `code` is `invalid_value` -- which it also
        # spends on a bad `size` and a bad `quality`. The sentence is the only thing
        # that separates a retired model from a rejected parameter here.
        (400, "The model 'gpt-image-0' does not exist.", "gpt-image-0", MODEL_NOT_FOUND, "gpt-image-0"),
    ],
    ids=["model gone", "model gone on a 400", "model gone on OpenAI"],
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
async def test_a_broker_names_the_upstream_that_actually_refused():
    """The live body from a geo-blocked render. OpenRouter routes one model id to
    several upstreams and names the one that answered in `error.metadata`; without it
    "User location is not supported" reads as Orb or OpenRouter refusing the user.

    It is also the fact that makes the failure *actionable*: the same request routed
    to a different upstream minutes later and rendered, so the fix is picking another
    model, not filing a bug.
    """
    handler = lambda _request: httpx.Response(  # noqa: E731
        400,
        json={
            "error": {
                "message": "User location is not supported for the API use.",
                "code": 400,
                "metadata": {"provider_name": "Google AI Studio"},
            }
        },
    )
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="openrouter", timeout=10)
    assert "Google AI Studio: User location is not supported" in str(exc.value)


@pytest.mark.asyncio
async def test_an_upstream_already_named_in_the_message_is_not_named_twice():
    handler = lambda _request: httpx.Response(  # noqa: E731
        400,
        json={"error": {"message": "Google AI Studio returned an error", "metadata": {"provider_name": "Google AI Studio"}}},
    )
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="openrouter", timeout=10)
    assert str(exc.value).count("Google AI Studio") == 1


@pytest.mark.asyncio
async def test_a_render_failure_is_user_facing_so_the_json_routes_may_relay_it():
    """The streaming path always relayed a provider's own words; regenerate, reroll
    and rehydrate answered "see server logs" for the identical failure. The subclass
    is what lets those routes tell an actionable rejection from an Orb defect."""
    from backend.workflows.errors import WorkflowUserFacingError

    handler = lambda _request: httpx.Response(400, json={"error": {"message": "Insufficient credits"}})  # noqa: E731
    with pytest.raises(WorkflowUserFacingError):
        await _client(handler).create_image("/images/generations", {"model": "m"}, provider_id="xai", timeout=10)


@pytest.mark.asyncio
async def test_a_numeric_error_code_does_not_break_the_code_reader():
    """OpenRouter answers `{"error": {"message": ..., "code": 404}}` -- an *int* where
    xAI and NanoGPT put a string. `_body_codes` skips it rather than choking, and the
    status alone carries the branch, so picking a chat model by mistake still reaches
    the user as the provider's own "No model found" rather than a 500."""
    handler = lambda _request: httpx.Response(  # noqa: E731
        404, json={"error": {"message": 'No model found for "deepseek/deepseek-v4-flash"', "code": 404}}
    )
    with pytest.raises(CloudImageError) as exc:
        await _client(handler).create_image(
            "/images/generations", {"model": "deepseek/deepseek-v4-flash"}, provider_id="openrouter", timeout=10
        )
    assert exc.value.kind == MODEL_NOT_FOUND
    assert "No model found" in str(exc.value)


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
