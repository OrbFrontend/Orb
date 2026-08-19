"""Asking the provider instead of tabulating it.

This replaced two hand-measured tables -- how many references each provider reads,
and which of its models read any -- that could never be finished against catalogues
of hundreds of models, and whose "not measured yet" default silently withheld a
capability the user had configured and paid for.

The refusal messages quoted below are the real ones, measured 2026-08-19 against
live keys. Every one of them was **free**: the provider refused before rendering
and billed nothing, which is the whole reason asking is cheaper than tabulating.
"""

from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine.contracts import (
    ImageGenerationError,
    ImageRequest,
    ImageResult,
    RenderTarget,
    ResolvedReference,
)
from backend.workflows.image_gen.engine.degrade import next_rung, trim
from backend.workflows.image_gen.engine.openai_image_client import CloudImageError
from backend.workflows.image_gen.engine.render import (
    MAX_DEGRADATIONS,
    resolve_and_generate,
)

# The real thing, from NanoGPT's `qwen-image` (max 3) handed five references.
TOO_MANY = "NanoGPT rejected the request (HTTP 400): Too many input images. This model accepts up to 3 input images."
# NanoGPT again, handed the one spelling it will not read.
NO_IMAGE = "NanoGPT rejected the request (HTTP 400): An image is required for image edits. missing_image_input"
# Together, on a model that has no image-to-image path.
UNSUPPORTED = "Together AI rejected the request (HTTP 400): Unsupported use of 'image_url' parameter"
# OpenAI, on a size its model does not take. Nothing to do with the references.
BAD_SIZE = "OpenAI rejected the request (HTTP 400): Supported sizes are 1024x1024, 1024x1536, 1536x1024, and auto."


def _reference(index: int = 0) -> ResolvedReference:
    return ResolvedReference(
        slot=("cloud", f"image_{index}"),
        source="cast",
        data=b"PNG",
        mime="image/png",
        origin=f"character:card-{index}",
        digest=str(index),
    )


def _refused(message: str, kind: str = "request") -> CloudImageError:
    return CloudImageError(message, kind)


# ── which refusals are worth another attempt ─────────────────────────────────


def test_a_named_limit_is_taken_at_its_word():
    """One retry lands on the answer instead of collapsing to zero references. The
    provider quoted its own limit; that is worth more than any guess."""
    rung = next_rung(_refused(TOO_MANY), sent=5, droppable=5)
    assert rung is not None and rung.keep == 3
    assert "accepts 3 reference images" in rung.note
    assert "2 of the 5 sent were left out" in rung.note


def test_an_image_refusal_with_no_number_drops_them_all():
    for message in (NO_IMAGE, UNSUPPORTED):
        rung = next_rung(_refused(message), sent=2, droppable=2)
        assert rung is not None and rung.keep == 0
        assert "rendered from the prompt alone" in rung.note


def test_a_refusal_that_is_not_about_images_is_left_alone():
    """Dropping a likeness would not fix a bad size, and would cost the user the thing
    they actually asked for. The failure stands and the message reaches them."""
    assert next_rung(_refused(BAD_SIZE), sent=2, droppable=2) is None


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "server", "model_not_found", ""])
def test_only_a_refusal_of_the_request_is_retried(kind):
    """A bad key, a rate limit and a 500 are about something other than what we sent.
    Retrying them with fewer references burns the user's quota to no purpose."""
    assert next_rung(_refused(TOO_MANY, kind), sent=3, droppable=3) is None


def test_a_backend_whose_slots_cannot_be_dropped_never_degrades():
    """A ComfyUI graph's image inputs are required -- rendering one unfilled submits
    whatever filename the workflow was exported with, and draws whatever that machine
    happened to have. No backend is named to arrange this; `droppable` is 0 because the
    slots said so."""
    assert next_rung(_refused(TOO_MANY), sent=2, droppable=0) is None


def test_a_byte_count_is_never_read_as_a_slot_count():
    """`image_input_too_large` quotes a byte budget. Reading 10485760 as a reference
    count would "trim" to a number larger than anything sent and loop."""
    huge = "rejected the request (HTTP 400): input image too large, max 10485760 bytes"
    rung = next_rung(_refused(huge), sent=3, droppable=3)
    assert rung is not None and rung.keep == 0


def test_a_limit_at_or_above_what_was_sent_is_not_a_limit():
    """A message echoing the count we sent is describing the problem, not the remedy."""
    echoed = "rejected the request (HTTP 400): 4 input images is too many for this image model"
    rung = next_rung(_refused(echoed), sent=4, droppable=4)
    assert rung is not None and rung.keep == 0


# ── trimming ─────────────────────────────────────────────────────────────────


def test_trimming_drops_from_the_end_because_the_list_is_positional():
    """Subject 0 is the render's primary and the one a solo slot must always resolve,
    so the last cast member is the right thing to lose first."""
    refs = [_reference(0), _reference(1), _reference(2)]
    kept = trim(refs, [True, True, True], 2)
    assert [reference.origin for reference in kept] == ["character:card-0", "character:card-1"]


def test_trimming_never_drops_a_required_reference_and_counts_it_against_the_budget():
    refs = [_reference(0), _reference(1), _reference(2)]
    kept = trim(refs, [False, True, True], 2)
    assert [reference.origin for reference in kept] == ["character:card-0", "character:card-1"]

    floor = trim(refs, [False, True, True], 0)
    assert [reference.origin for reference in floor] == ["character:card-0"]


# ── the ladder, end to end ───────────────────────────────────────────────────


def _target(count: int, *, required: bool = False) -> RenderTarget:
    return RenderTarget(
        source="cloud",
        target_id="",
        model="m",
        supports_negative_prompt=False,
        supports_seed=False,
        supports_dimensions=True,
        width=1024,
        height=1024,
        reference_slots=tuple({"slot": ["cloud", f"image_{i}"], "source": "cast", "required": required} for i in range(count)),
        reference_capacity=count,
    )


def _request(count: int) -> ImageRequest:
    return ImageRequest(
        prompt="p", negative_prompt="", seed=1, style_id="s", references=tuple(_reference(i) for i in range(count))
    )


class _Adapter:
    """Refuses with `script`, one entry per attempt, then renders."""

    def __init__(self, script: list[str | None]):
        self.script = list(script)
        self.seen: list[int] = []

    async def generate(self, request, *, target, progress=None):
        self.seen.append(len(request.references))
        message = self.script.pop(0) if self.script else None
        if message is not None:
            raise _refused(message)
        return ImageResult(image_bytes=b"PNG", mime="image/png", backend_info={"notes": ["rendered"]})


async def test_the_ladder_retries_at_the_named_limit_and_discloses_it():
    adapter = _Adapter([TOO_MANY, None])
    result = await resolve_and_generate(adapter, _request(5), target=_target(5))

    assert adapter.seen == [5, 3]
    notes = result.backend_info["notes"]
    assert "accepts 3 reference images" in notes[0]
    # The degradation explains the render the other notes then describe.
    assert notes[-1] == "rendered"


async def test_the_ladder_falls_all_the_way_to_none_and_says_so():
    adapter = _Adapter([TOO_MANY, NO_IMAGE, None])
    result = await resolve_and_generate(adapter, _request(4), target=_target(4))

    assert adapter.seen == [4, 3, 0]
    assert any("rendered from the prompt alone" in note for note in result.backend_info["notes"])


async def test_the_ladder_is_bounded():
    """A provider that concedes one slot at a time could walk this forever, so the
    bound is the ladder's and not the arithmetic's: one attempt plus `MAX_DEGRADATIONS`
    retries, then the refusal stands and the user sees it."""
    grudging = [f"rejected the request (HTTP 400): accepts up to {n} input images" for n in (4, 3, 2, 1)]
    adapter = _Adapter([*grudging, None])

    with pytest.raises(ImageGenerationError):
        await resolve_and_generate(adapter, _request(5), target=_target(5))

    assert adapter.seen == [5, 4, 3]
    assert len(adapter.seen) == MAX_DEGRADATIONS + 1


async def test_a_failure_that_is_not_degradable_is_raised_untouched():
    adapter = _Adapter([BAD_SIZE, None])
    with pytest.raises(ImageGenerationError) as excinfo:
        await resolve_and_generate(adapter, _request(2), target=_target(2))

    assert "Supported sizes" in str(excinfo.value)
    assert adapter.seen == [2], "a non-reference refusal must not cost a second attempt"


async def test_a_required_slot_fails_rather_than_rendering_without_its_image():
    adapter = _Adapter([TOO_MANY, None])
    with pytest.raises(ImageGenerationError):
        await resolve_and_generate(adapter, _request(2), target=_target(2, required=True))
    assert adapter.seen == [2]


async def test_a_render_that_never_refuses_is_untouched():
    adapter = _Adapter([None])
    result = await resolve_and_generate(adapter, _request(2), target=_target(2))

    assert adapter.seen == [2]
    assert result.backend_info["notes"] == ["rendered"]
