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
from backend.workflows.image_gen.engine.degrade import DROPPABLE_FIELDS, next_rung, trim
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
# Together, on FLUX.2-pro -- the same provider whose FLUX.1 default takes this field
# and ignores it. A per-model list of who accepts a negative prompt is the table this
# module exists to not keep, so the model is asked and answers for free.
NO_NEGATIVE = (
    "Together AI rejected the request (HTTP 400): Invalid parameter detected. "
    "The parameter \"'negative_prompt'\" is not recognized or supported."
)


# Together AI, handed the 64-bit seed Orb draws for every render. Measured live
# 2026-09-06 on `Wan-AI/Wan2.6-image`, and free like the rest: the 400 arrives before
# anything is rendered. The bound is inclusive at both ends -- 0 and 2147483647 got
# through to the next check, -1 and 2147483648 came back with this same message -- and
# it is reached only *after* the model resolves, so it is the model's answer and not
# something a preset column could have declared in advance.
OOB_SEED = (
    "Together AI rejected the request (HTTP 400): Invalid value for 'seed' parameter. "
    "Seed must be an integer value between 0 and 2147483647. Default: Random."
)
# The seed Orb had drawn when this was measured -- larger than the whole range, which
# is the ordinary case rather than an edge one.
BIG_SEED = 12297829382473034410
TOGETHER_MAX = 2147483647


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


def test_a_named_field_is_dropped_and_the_references_are_left_alone():
    """The provider named one of Orb's own optional fields, so that is what goes --
    the references were not what it refused."""
    rung = next_rung(_refused(NO_NEGATIVE), sent=3, droppable=3, sending=("negative_prompt",))
    assert rung is not None and rung.drop == "negative_prompt"
    assert rung.keep == 3, "a field refusal must not also cost a likeness"
    assert rung.note == DROPPABLE_FIELDS["negative_prompt"]


def test_a_field_refusal_is_answerable_with_no_references_in_hand():
    """The attempt a reference rung has just left behind carries none, and it is
    exactly the one that then gets refused for the parameter."""
    rung = next_rung(_refused(NO_NEGATIVE), sent=0, droppable=0, sending=("negative_prompt",))
    assert rung is not None and rung.drop == "negative_prompt"


def test_a_field_that_was_not_sent_is_never_dropped():
    """`sending` is read off the attempt, so a field already given up cannot be given
    up again -- which is what stops the ladder walking in place."""
    assert next_rung(_refused(NO_NEGATIVE), sent=2, droppable=2, sending=()) is None


def test_a_field_name_is_matched_whole():
    """A neighbouring parameter that merely ends in one of ours is a different field,
    and dropping ours would not answer the refusal."""
    other = "rejected the request (HTTP 400): the parameter 'default_negative_prompt' is not supported"
    assert next_rung(_refused(other), sent=0, droppable=0, sending=("negative_prompt",)) is None


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


def _request(count: int, *, negative: str = "", seed: int = 1) -> ImageRequest:
    return ImageRequest(
        prompt="p", negative_prompt=negative, seed=seed, style_id="s", references=tuple(_reference(i) for i in range(count))
    )


class _Adapter:
    """Refuses with `script`, one entry per attempt, then renders."""

    def __init__(self, script: list[str | None]):
        self.script = list(script)
        self.seen: list[int] = []
        self.negatives: list[str] = []
        self.seeds: list[int] = []

    async def generate(self, request, *, target, progress=None):
        self.seen.append(len(request.references))
        self.negatives.append(request.negative_prompt)
        self.seeds.append(request.seed)
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
    sent = MAX_DEGRADATIONS + 3
    grudging = [f"rejected the request (HTTP 400): accepts up to {n} input images" for n in range(sent - 1, 0, -1)]
    adapter = _Adapter([*grudging, None])

    with pytest.raises(ImageGenerationError):
        await resolve_and_generate(adapter, _request(sent), target=_target(sent))

    # One conceded slot per rung, and then the refusal stands with references still in
    # hand -- derived from the bound rather than written out, so a new droppable field
    # moves the bound here too instead of quietly reading as a regression.
    assert adapter.seen == list(range(sent, sent - MAX_DEGRADATIONS - 1, -1))
    assert len(adapter.seen) == MAX_DEGRADATIONS + 1


async def test_the_ladder_gives_up_the_references_then_the_field_the_provider_named():
    """Together's FLUX.2 refuses both, one at a time: `image_url` first, then
    `negative_prompt` on the referenceless retry. Two rungs, one render, both said."""
    adapter = _Adapter([UNSUPPORTED, NO_NEGATIVE, None])
    result = await resolve_and_generate(adapter, _request(2, negative="blurry"), target=_target(2))

    assert adapter.seen == [2, 0, 0]
    assert adapter.negatives == ["blurry", "blurry", ""]
    notes = result.backend_info["notes"]
    assert "rendered from the prompt alone" in notes[0]
    assert notes[1] == DROPPABLE_FIELDS["negative_prompt"]


async def test_a_named_field_goes_without_costing_the_references():
    adapter = _Adapter([NO_NEGATIVE, None])
    result = await resolve_and_generate(adapter, _request(2, negative="blurry"), target=_target(2))

    assert adapter.seen == [2, 2], "the likenesses survive a refusal that was not about them"
    assert adapter.negatives == ["blurry", ""]
    assert result.backend_info["notes"][0] == DROPPABLE_FIELDS["negative_prompt"]


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


# ── a seed the provider will not take ────────────────────────────────────────


def test_a_quoted_seed_range_is_taken_at_its_word():
    """The whole fix in one call: Together names its range, Orb folds into it.

    Nothing else about the request moves -- the references it was handed are still
    the references it sends -- because a seed is not what was refused *for*.
    """
    rung = next_rung(_refused(OOB_SEED), sent=2, droppable=2, seed=BIG_SEED)
    assert rung is not None and rung.seed is not None
    assert 0 <= rung.seed <= TOGETHER_MAX
    assert rung.keep == 2 and rung.drop == ""


def test_the_refit_seed_is_disclosed_by_the_number_and_not_by_a_note():
    """A note here would fire on every single render against this provider, and a
    disclosure that always fires is one users stop reading. What the user gets instead
    is the seed that actually rendered, recorded on the attachment."""
    rung = next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=BIG_SEED)
    assert rung is not None and rung.note == ""


def test_the_refit_is_idempotent_so_the_recorded_seed_reproduces_the_image():
    """Folded, not clamped, and folded once: replaying the recorded seed through the
    same refusal has to land on the same number, or the seed shown next to the image
    is a number that draws a different picture."""
    first = next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=BIG_SEED)
    assert first is not None and first.seed is not None
    assert next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=first.seed) is None


def test_a_seed_already_inside_the_quoted_range_leaves_the_failure_standing():
    """The refusal names a range this seed is already in, so it is talking about
    something else. Resending the same number would spend a rung to be told the same
    thing -- the ladder must not walk in place."""
    assert next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=7) is None


def test_a_seed_refusal_that_quotes_no_range_is_raised_untouched():
    """Nothing to fold into. Guessing a bound would be the per-provider table this
    module exists to avoid, so the provider's own words reach the user instead."""
    vague = "Together AI rejected the request (HTTP 400): Invalid value for 'seed' parameter."
    assert next_rung(_refused(vague), sent=0, droppable=0, seed=BIG_SEED) is None


def test_orbs_own_http_status_is_never_read_as_a_seed_bound():
    """`_say` puts "(HTTP 400)" in front of every provider message, so a rule that
    scavenged bare integers would fold a good seed into [0, 400] and render the wrong
    picture rather than fail. Both readings are anchored on words instead."""
    assert next_rung(_refused(BAD_SIZE + " seed"), sent=0, droppable=0, seed=BIG_SEED) is None


def test_an_exclusive_ceiling_stops_one_short_of_the_number_it_names():
    """`less than N` is not `at most N`, and landing exactly on N would be refused
    again with no rung left to answer it."""
    message = "Provider rejected the request (HTTP 400): seed must be less than 1000"
    rung = next_rung(_refused(message), sent=0, droppable=0, seed=BIG_SEED)
    assert rung is not None and rung.seed is not None and rung.seed <= 999

    inclusive = "Provider rejected the request (HTTP 400): seed must be less than or equal to 1000"
    rung = next_rung(_refused(inclusive), sent=0, droppable=0, seed=1000)
    assert rung is None  # 1000 is already inside [0, 1000]


def test_a_reference_limit_is_never_mistaken_for_a_seed_bound():
    """The two readings share a message space, so the guard runs both ways: a refusal
    that quotes a count and never says `seed` is a reference rung, unchanged."""
    rung = next_rung(_refused(TOO_MANY), sent=5, droppable=5, seed=BIG_SEED)
    assert rung is not None and rung.seed is None and rung.keep == 3


async def test_the_ladder_refits_the_seed_and_keeps_everything_the_user_configured():
    """End to end on the reported failure: one refusal, one retry, the render lands.

    The references and the negative prompt both survive -- this is the one rung that
    gives up nothing -- and there is nothing to disclose, so the notes are the
    render's own.
    """
    adapter = _Adapter([OOB_SEED, None])
    result = await resolve_and_generate(adapter, _request(2, negative="blurry", seed=BIG_SEED), target=_target(2))

    assert adapter.seen == [2, 2]
    assert adapter.negatives == ["blurry", "blurry"]
    assert adapter.seeds[0] == BIG_SEED
    assert 0 <= adapter.seeds[1] <= TOGETHER_MAX
    assert result.backend_info["notes"] == ["rendered"]


async def test_a_refused_seed_and_a_refused_reference_are_answered_one_at_a_time():
    """The rungs compose: Together refuses the seed first, then the model refuses the
    reference. Only the second costs the user something, and only it is disclosed."""
    adapter = _Adapter([OOB_SEED, UNSUPPORTED, None])
    result = await resolve_and_generate(adapter, _request(2, seed=BIG_SEED), target=_target(2))

    assert adapter.seen == [2, 2, 0]
    assert adapter.seeds[1] == adapter.seeds[2] <= TOGETHER_MAX
    notes = result.backend_info["notes"]
    assert "rendered from the prompt alone" in notes[0]
    assert len(notes) == 2 and notes[-1] == "rendered"
