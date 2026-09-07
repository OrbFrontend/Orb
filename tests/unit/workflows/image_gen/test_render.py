"""The render seam applies explicit settings without interpreting remote prose."""

from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine.contracts import (
    ImageRequest,
    ImageResult,
    RenderTarget,
)
from backend.workflows.image_gen.engine.openai_image_client import CloudImageError
from backend.workflows.image_gen.engine.render import resolve_and_generate


def _request(seed: int = 1) -> ImageRequest:
    return ImageRequest(prompt="p", negative_prompt="blurry", seed=seed, style_id="s")


def _target(*, supports_seed: bool = True, seed_max: int | None = None) -> RenderTarget:
    return RenderTarget(
        source="cloud",
        target_id="",
        model="m",
        supports_negative_prompt=True,
        supports_seed=supports_seed,
        supports_dimensions=True,
        width=1024,
        height=1024,
        seed_max=seed_max,
    )


class _Adapter:
    def __init__(self, failure: CloudImageError | None = None) -> None:
        self.failure = failure
        self.requests: list[ImageRequest] = []

    async def generate(self, request, *, target, progress=None):
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return ImageResult(image_bytes=b"PNG", mime="image/png", backend_info={"seed": request.seed})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message, kind",
    [
        ("seed must be between 0 and 2147483647", "request"),
        ("negative_prompt is unsupported", "request"),
        ("only one reference image is allowed", "request"),
        ("daily quota exhausted", "rate_limit"),
    ],
)
async def test_remote_messages_are_raised_after_one_attempt(message, kind):
    failure = CloudImageError(message, kind)
    adapter = _Adapter(failure)

    with pytest.raises(CloudImageError) as exc:
        await resolve_and_generate(adapter, _request(2**63), target=_target())

    assert exc.value is failure
    assert len(adapter.requests) == 1
    assert adapter.requests[0].seed == 2**63


@pytest.mark.asyncio
async def test_a_user_configured_seed_ceiling_is_applied_before_the_only_attempt():
    adapter = _Adapter()
    maximum = 2**31 - 1

    result = await resolve_and_generate(adapter, _request(2**63), target=_target(seed_max=maximum))

    assert len(adapter.requests) == 1
    assert 0 <= adapter.requests[0].seed <= maximum
    assert result.backend_info["seed"] == adapter.requests[0].seed


@pytest.mark.asyncio
async def test_a_seed_ceiling_does_not_make_a_seedless_provider_send_one():
    adapter = _Adapter()
    original = 2**63

    await resolve_and_generate(adapter, _request(original), target=_target(supports_seed=False, seed_max=10))

    assert adapter.requests[0].seed == original
