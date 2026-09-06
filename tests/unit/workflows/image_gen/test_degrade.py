"""Tests for bounded provider fallbacks."""

from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine import render
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
    MAX_RATE_LIMIT_WAITS,
    RATE_LIMIT_PAUSE_SECONDS,
    RETRY_PAUSE_SECONDS,
    resolve_and_generate,
)

# Provider refusal examples.
TOO_MANY = "NanoGPT rejected the request (HTTP 400): Too many input images. This model accepts up to 3 input images."
NO_IMAGE = "NanoGPT rejected the request (HTTP 400): An image is required for image edits. missing_image_input"
UNSUPPORTED = "Together AI rejected the request (HTTP 400): Unsupported use of 'image_url' parameter"
BAD_SIZE = "OpenAI rejected the request (HTTP 400): Supported sizes are 1024x1024, 1024x1536, 1536x1024, and auto."
NO_NEGATIVE = (
    "Together AI rejected the request (HTTP 400): Invalid parameter detected. "
    "The parameter \"'negative_prompt'\" is not recognized or supported."
)


OOB_SEED = (
    "Together AI rejected the request (HTTP 400): Invalid value for 'seed' parameter. "
    "Seed must be an integer value between 0 and 2147483647. Default: Random."
)
BIG_SEED = 12297829382473034410
TOGETHER_MAX = 2147483647

BAD_AREA = (
    "Together AI rejected the request (HTTP 400): Invalid dimensions. The total area "
    "(width × height) must be within the range of 1265×1265 to 1440×1440. The aspect "
    "ratio must be between 1:4 and 4:1. For example, 768*2700 is a valid resolution."
)
WAN_WINDOW = (1265 * 1265, 1440 * 1440)

SIZE_MENU = (
    "Together AI rejected the request (HTTP 400): Unsupported use of width/height parameters. "
    "The specified dimensions are not supported for the selected model. Supported values are: "
    "'1024x1024', '1264x848', '848x1264', '1200x896', '896x1200', '928x1152', '1152x928', "
    "'768x1376', '1376x768', '1584x672', '2048x512', '512x2048', '3072x384', '384x3072'."
)

RATE_LIMITED_429 = (
    "Together AI rejected the request (HTTP 429): Too many requests in a short window. "
    "Our rate limits are dynamic - they shift with live model capacity and your traffic shape, "
    "so steady traffic and exponential back-off help."
)


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


@pytest.fixture(autouse=True)
def waits(monkeypatch) -> list[float]:
    taken: list[float] = []

    async def record(seconds: float) -> None:
        taken.append(seconds)

    monkeypatch.setattr(render, "_pause", record)
    return taken


# ── which refusals are worth another attempt ─────────────────────────────────


def test_a_named_limit_is_taken_at_its_word():
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
    assert next_rung(_refused(BAD_SIZE), sent=2, droppable=2) is None


def test_a_named_field_is_dropped_and_the_references_are_left_alone():
    rung = next_rung(_refused(NO_NEGATIVE), sent=3, droppable=3, sending=("negative_prompt",))
    assert rung is not None and rung.drop == "negative_prompt"
    assert rung.keep == 3, "a field refusal must not also cost a likeness"
    assert rung.note == DROPPABLE_FIELDS["negative_prompt"]


def test_a_field_refusal_is_answerable_with_no_references_in_hand():
    rung = next_rung(_refused(NO_NEGATIVE), sent=0, droppable=0, sending=("negative_prompt",))
    assert rung is not None and rung.drop == "negative_prompt"


def test_a_field_that_was_not_sent_is_never_dropped():
    assert next_rung(_refused(NO_NEGATIVE), sent=2, droppable=2, sending=()) is None


def test_a_field_name_is_matched_whole():
    other = "rejected the request (HTTP 400): the parameter 'default_negative_prompt' is not supported"
    assert next_rung(_refused(other), sent=0, droppable=0, sending=("negative_prompt",)) is None


@pytest.mark.parametrize("kind", ["auth", "rate_limit", "server", "model_not_found", ""])
def test_only_a_refusal_of_the_request_is_retried(kind):
    assert next_rung(_refused(TOO_MANY, kind), sent=3, droppable=3) is None


def test_a_backend_whose_slots_cannot_be_dropped_never_degrades():
    assert next_rung(_refused(TOO_MANY), sent=2, droppable=0) is None


def test_a_byte_count_is_never_read_as_a_slot_count():
    huge = "rejected the request (HTTP 400): input image too large, max 10485760 bytes"
    rung = next_rung(_refused(huge), sent=3, droppable=3)
    assert rung is not None and rung.keep == 0


def test_a_limit_at_or_above_what_was_sent_is_not_a_limit():
    echoed = "rejected the request (HTTP 400): 4 input images is too many for this image model"
    rung = next_rung(_refused(echoed), sent=4, droppable=4)
    assert rung is not None and rung.keep == 0


# ── trimming ─────────────────────────────────────────────────────────────────


def test_trimming_drops_from_the_end_because_the_list_is_positional():
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


def _target(count: int, *, required: bool = False, size: tuple[int, int] = (1024, 1024)) -> RenderTarget:
    return RenderTarget(
        source="cloud",
        target_id="",
        model="m",
        supports_negative_prompt=False,
        supports_seed=False,
        supports_dimensions=True,
        width=size[0],
        height=size[1],
        reference_slots=tuple({"slot": ["cloud", f"image_{i}"], "source": "cast", "required": required} for i in range(count)),
        reference_capacity=count,
    )


def _request(count: int, *, negative: str = "", seed: int = 1) -> ImageRequest:
    return ImageRequest(
        prompt="p", negative_prompt=negative, seed=seed, style_id="s", references=tuple(_reference(i) for i in range(count))
    )


class _Adapter:
    def __init__(self, script: list[str | None]):
        self.script = list(script)
        self.seen: list[int] = []
        self.negatives: list[str] = []
        self.seeds: list[int] = []
        self.sizes: list[tuple[int | None, int | None]] = []

    async def generate(self, request, *, target, progress=None):
        self.seen.append(len(request.references))
        self.negatives.append(request.negative_prompt)
        self.seeds.append(request.seed)
        self.sizes.append((target.width, target.height))
        entry = self.script.pop(0) if self.script else None
        if entry is not None:
            raise _refused(*entry) if isinstance(entry, tuple) else _refused(entry)
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
    sent = MAX_DEGRADATIONS + 3
    grudging = [f"rejected the request (HTTP 400): accepts up to {n} input images" for n in range(sent - 1, 0, -1)]
    adapter = _Adapter([*grudging, None])

    with pytest.raises(ImageGenerationError):
        await resolve_and_generate(adapter, _request(sent), target=_target(sent))

    assert adapter.seen == list(range(sent, sent - MAX_DEGRADATIONS - 1, -1))
    assert len(adapter.seen) == MAX_DEGRADATIONS + 1


async def test_the_ladder_gives_up_the_references_then_the_field_the_provider_named():
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
    rung = next_rung(_refused(OOB_SEED), sent=2, droppable=2, seed=BIG_SEED)
    assert rung is not None and rung.seed is not None
    assert 0 <= rung.seed <= TOGETHER_MAX
    assert rung.keep == 2 and rung.drop == ""


def test_the_refit_seed_is_disclosed_by_the_number_and_not_by_a_note():
    rung = next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=BIG_SEED)
    assert rung is not None and rung.note == ""


def test_the_refit_is_idempotent_so_the_recorded_seed_reproduces_the_image():
    first = next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=BIG_SEED)
    assert first is not None and first.seed is not None
    assert next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=first.seed) is None


def test_a_seed_already_inside_the_quoted_range_leaves_the_failure_standing():
    assert next_rung(_refused(OOB_SEED), sent=0, droppable=0, seed=7) is None


def test_a_seed_refusal_that_quotes_no_range_is_raised_untouched():
    vague = "Together AI rejected the request (HTTP 400): Invalid value for 'seed' parameter."
    assert next_rung(_refused(vague), sent=0, droppable=0, seed=BIG_SEED) is None


def test_orbs_own_http_status_is_never_read_as_a_seed_bound():
    assert next_rung(_refused(BAD_SIZE + " seed"), sent=0, droppable=0, seed=BIG_SEED) is None


def test_an_exclusive_ceiling_stops_one_short_of_the_number_it_names():
    message = "Provider rejected the request (HTTP 400): seed must be less than 1000"
    rung = next_rung(_refused(message), sent=0, droppable=0, seed=BIG_SEED)
    assert rung is not None and rung.seed is not None and rung.seed <= 999

    inclusive = "Provider rejected the request (HTTP 400): seed must be less than or equal to 1000"
    rung = next_rung(_refused(inclusive), sent=0, droppable=0, seed=1000)
    assert rung is None


def test_a_reference_limit_is_never_mistaken_for_a_seed_bound():
    rung = next_rung(_refused(TOO_MANY), sent=5, droppable=5, seed=BIG_SEED)
    assert rung is not None and rung.seed is None and rung.keep == 3


async def test_the_ladder_refits_the_seed_and_keeps_everything_the_user_configured():
    adapter = _Adapter([OOB_SEED, None])
    result = await resolve_and_generate(adapter, _request(2, negative="blurry", seed=BIG_SEED), target=_target(2))

    assert adapter.seen == [2, 2]
    assert adapter.negatives == ["blurry", "blurry"]
    assert adapter.seeds[0] == BIG_SEED
    assert 0 <= adapter.seeds[1] <= TOGETHER_MAX
    assert result.backend_info["notes"] == ["rendered"]


async def test_a_refused_seed_and_a_refused_reference_are_answered_one_at_a_time():
    adapter = _Adapter([OOB_SEED, UNSUPPORTED, None])
    result = await resolve_and_generate(adapter, _request(2, seed=BIG_SEED), target=_target(2))

    assert adapter.seen == [2, 2, 0]
    assert adapter.seeds[1] == adapter.seeds[2] <= TOGETHER_MAX
    notes = result.backend_info["notes"]
    assert "rendered from the prompt alone" in notes[0]
    assert len(notes) == 2 and notes[-1] == "rendered"


# ── asking again without being read as traffic ───────────────────────────────


async def test_every_retry_is_paced(waits):
    adapter = _Adapter([OOB_SEED, TOO_MANY, None])
    await resolve_and_generate(adapter, _request(5, seed=BIG_SEED), target=_target(5))

    assert len(adapter.seen) == 3
    assert waits == [RETRY_PAUSE_SECONDS, RETRY_PAUSE_SECONDS], "one wait before each retry, none before the first call"


async def test_a_rate_limit_is_waited_out_and_the_same_request_is_sent_again(waits):
    adapter = _Adapter([(RATE_LIMITED_429, "rate_limit"), None])
    result = await resolve_and_generate(adapter, _request(2, negative="blurry", seed=7), target=_target(2))

    assert adapter.seen == [2, 2] and adapter.negatives == ["blurry", "blurry"] and adapter.seeds == [7, 7]
    assert waits == [RATE_LIMIT_PAUSE_SECONDS]
    assert result.backend_info["notes"] == ["rendered"], "waiting is not a degradation, so there is nothing to disclose"


async def test_a_provider_that_keeps_asking_for_less_traffic_is_not_waited_out_forever(waits):
    adapter = _Adapter([(RATE_LIMITED_429, "rate_limit")] * 6)
    with pytest.raises(ImageGenerationError) as raised:
        await resolve_and_generate(adapter, _request(0), target=_target(0))

    assert len(adapter.seen) == MAX_RATE_LIMIT_WAITS + 1
    assert waits == [RATE_LIMIT_PAUSE_SECONDS] * MAX_RATE_LIMIT_WAITS
    assert "429" in str(raised.value)


# ── a resolution the model will not render ───────────────────────────────────


def test_a_quoted_area_window_rescales_the_request_into_it():
    rung = next_rung(_refused(BAD_AREA), sent=0, droppable=0, size=(1024, 1536))
    assert rung is not None and rung.size is not None
    low, high = WAN_WINDOW
    assert low <= rung.size[0] * rung.size[1] <= high
    assert abs(rung.size[0] / rung.size[1] - 1024 / 1536) < 0.01
    assert "rendered at" in rung.note


def test_an_area_already_inside_the_window_leaves_the_failure_standing():
    assert next_rung(_refused(BAD_AREA), sent=0, droppable=0, size=(1300, 1300)) is None


def test_a_size_refusal_never_costs_a_reference():
    message = "Provider rejected the request (HTTP 400): image dimensions must be within the range of 1265×1265 to 1440×1440"
    rung = next_rung(_refused(message), sent=3, droppable=3, size=(1024, 1536))
    assert rung is not None and rung.size is not None and rung.keep == 3


def test_a_menu_of_sizes_is_answered_by_the_nearest_one_offered():
    rung = next_rung(_refused(SIZE_MENU), sent=0, droppable=0, size=(1024, 1536))
    assert rung is not None and rung.size == (848, 1264)


async def test_the_ladder_answers_the_seed_then_the_size_and_renders():
    adapter = _Adapter([OOB_SEED, BAD_AREA, None])
    target = _target(0)
    result = await resolve_and_generate(adapter, _request(0, seed=BIG_SEED), target=target)

    assert adapter.seeds[0] == BIG_SEED
    assert 0 <= adapter.seeds[1] <= TOGETHER_MAX
    assert adapter.sizes[0] == adapter.sizes[1] == (target.width, target.height)
    low, high = WAN_WINDOW
    assert low <= adapter.sizes[2][0] * adapter.sizes[2][1] <= high
    notes = result.backend_info["notes"]
    assert len(notes) == 2 and "was rendered at" in notes[0]


# ── not paying for the same refusal twice ────────────────────────────────────


async def test_what_was_learned_once_is_applied_before_the_first_attempt():
    adapter = _Adapter([None])
    known = {"seed_low": 0, "seed_high": TOGETHER_MAX, "sizes": {"1024x1536": "848x1264"}}
    target = _target(0, size=(1024, 1536))

    await resolve_and_generate(adapter, _request(0, seed=BIG_SEED), target=target, known=known)

    assert len(adapter.seen) == 1, "a remembered target must not be re-probed"
    assert 0 <= adapter.seeds[0] <= TOGETHER_MAX
    assert adapter.sizes[0] == (848, 1264)


async def test_a_remembered_resize_is_disclosed_every_time_it_applies():
    adapter = _Adapter([None])
    result = await resolve_and_generate(adapter, _request(0), target=_target(0), known={"sizes": {"1024x1024": "848x1264"}})
    assert "does not render at 1024x1024" in result.backend_info["notes"][0]
    assert "rendered at 848x1264" in result.backend_info["notes"][0]


async def test_the_ladder_reports_the_bounds_it_had_to_discover():
    adapter = _Adapter([OOB_SEED, SIZE_MENU, None])
    target = _target(0, size=(1024, 1536))
    result = await resolve_and_generate(adapter, _request(0, seed=BIG_SEED), target=target)

    learned = result.backend_info["learned"]
    assert learned["seed_high"] == TOGETHER_MAX and learned["seed_low"] == 0
    assert learned["sizes"] == {f"{target.width}x{target.height}": "848x1264"}


async def test_a_stale_memory_is_corrected_rather_than_obeyed():
    adapter = _Adapter([OOB_SEED, None])
    result = await resolve_and_generate(adapter, _request(0, seed=BIG_SEED), target=_target(0), known={"seed_high": 2**40})
    assert adapter.seeds[0] == BIG_SEED % (2**40 + 1), "the stale bound is applied first"
    assert 0 <= adapter.seeds[1] <= TOGETHER_MAX
    assert result.backend_info["learned"]["seed_high"] == TOGETHER_MAX


@pytest.mark.parametrize(
    "known",
    [
        {"seed_high": "2147483647"},
        {"seed_high": True},
        {"sizes": {"1024x1024": "not-a-size"}},
        {"sizes": "1024x1024"},
        {"sizes": {"1024x1024": "1024x1024"}},
    ],
)
async def test_a_malformed_memory_is_ignored_rather_than_believed(known):
    adapter = _Adapter([None])
    result = await resolve_and_generate(adapter, _request(0, seed=BIG_SEED), target=_target(0), known=known)

    assert adapter.seeds[0] == BIG_SEED
    assert adapter.sizes[0] == (1024, 1024)
    assert result.backend_info.get("notes") == ["rendered"]
