"""The preset table and the pure request builders that read it.

xAI *silently ignores* unknown fields, so the API never tells you a parameter was
wrong: sending everything and letting the server sort it out is the difference
between a working negative prompt and one the user watches have no effect. Hence
the allowlist, and hence most of the assertions below being about what is absent.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from backend.workflows.image_gen.engine.contracts import ResolvedReference
from backend.workflows.image_gen.engine.providers import (
    PRESETS,
    aspect_for,
    build_edit_body,
    build_generation_body,
    get_preset,
    pixels_for,
    provider_catalogue,
    takes_references,
)

XAI = get_preset("xai")
assert XAI is not None

TOGETHER = get_preset("togetherai")
assert TOGETHER is not None


def test_every_preset_endpoint_is_https():
    for preset in PRESETS:
        if not preset.base_url:
            # `custom` has none by design; the config normalizer is what refuses a
            # plaintext or credentialed URL for it.
            assert preset.id == "custom"
            continue
        parsed = urlsplit(preset.base_url)
        assert parsed.scheme == "https", preset.id
        assert not parsed.username and not parsed.password, preset.id
    # An unverified row is a guess from vendor docs, and saying so in the table is
    # what keeps the next person from trusting it as measured fact.
    assert [preset.id for preset in PRESETS if preset.verified] == ["xai", "togetherai"]


def test_a_width_height_preset_declares_the_grid_it_snaps_to():
    """`pixels_for` divides by the step and clamps to the bounds, so a row that
    declares the mode without them would emit whatever it was handed -- and the
    provider answers a 400, not an image."""
    for preset in PRESETS:
        if preset.dimension_mode != "width_height":
            continue
        assert preset.dimension_step > 0, preset.id
        assert preset.max_dimension >= preset.min_dimension > 0, preset.id


def test_the_catalogue_projects_the_table_and_carries_no_credential():
    catalogue = provider_catalogue()
    assert {row["id"] for row in catalogue} == {preset.id for preset in PRESETS}
    for row in catalogue:
        assert "api_key" not in row and "key" not in row
    assert next(row for row in catalogue if row["id"] == "custom")["needs_base_url"] is True


# ── the allowlist ────────────────────────────────────────────────────────────


def _xai_body(**kwargs) -> dict:
    return build_generation_body(XAI, model="grok-imagine-image", prompt="a quiet room", **kwargs).body


def test_xai_never_receives_size_even_though_it_is_the_openai_spelling():
    """xAI rejects it outright ("Argument not supported: size"). That is the polite
    failure; the impolite one is a provider that accepts and ignores it."""
    body = _xai_body(width=1024, height=1024)
    assert "size" not in body
    assert body["aspect_ratio"] == "1:1"


@pytest.mark.parametrize("preset", PRESETS, ids=[preset.id for preset in PRESETS])
def test_no_preset_emits_a_field_it_does_not_declare(preset):
    body = build_generation_body(
        preset,
        model="m",
        prompt="p",
        negative_prompt="blurry, extra fingers",
        seed=42,
        quality="high",
        width=1024,
        height=1536,
    ).body
    if not preset.supports_negative_prompt:
        assert "negative_prompt" not in body
    if not preset.supports_seed:
        assert "seed" not in body
    if not preset.supports_quality:
        assert "quality" not in body
    if preset.dimension_mode != "size":
        assert "size" not in body
    if preset.dimension_mode != "aspect_ratio":
        assert "aspect_ratio" not in body
    if preset.dimension_mode != "width_height":
        assert "width" not in body and "height" not in body
    # Never, on any provider: moderation is team-gated on xAI and hard-fails the
    # call; `user` is a stable identifier shipped to a third party for no benefit;
    # `style` would double-apply, since Orb styles already inject prompt text.
    assert "moderation" not in body
    assert "user" not in body
    assert "style" not in body
    # `n` is the field that silently multiplies the bill.
    assert body["n"] == 1


def test_a_declaring_provider_does_receive_the_optional_fields():
    """The allowlist has to be a filter, not a blanket refusal -- otherwise it would
    pass the test above by sending nothing at all."""
    body = build_generation_body(
        TOGETHER, model="m", prompt="p", negative_prompt="blurry", seed=7, width=1024, height=1024
    ).body
    assert body["negative_prompt"] == "blurry"
    assert body["seed"] == 7
    # Integers, not `size`: the live API accepts `size`, ignores it, and renders the
    # model default -- so the declared-from-docs spelling produced a request that
    # succeeded, disclosed a resolution, and returned a different one.
    assert (body["width"], body["height"]) == (1024, 1024)
    assert "size" not in body


# ── the pixel grid ───────────────────────────────────────────────────────────


def test_a_size_on_the_grid_passes_through_and_says_nothing():
    assert pixels_for(TOGETHER, 1024, 576) == (1024, 576, None)


def test_an_oversized_request_keeps_its_aspect_ratio():
    """Scaled down whole rather than clamped per edge: clamping 2560x1440 would give
    1792x1440, turning a 16:9 request into a near-square and cropping the framing
    the prompt was written for."""
    width, height, note = pixels_for(TOGETHER, 2560, 1440)
    assert (width, height) == (1792, 1008)
    assert abs((width / height) - (2560 / 1440)) < 0.01
    assert note and "2560x1440" in note and "1792x1008" in note


def test_an_off_grid_edge_is_snapped_to_the_step():
    """Together 400s on a non-multiple of 16 rather than rounding for you."""
    width, height, note = pixels_for(TOGETHER, 500, 500)
    assert width % TOGETHER.dimension_step == 0
    assert height % TOGETHER.dimension_step == 0
    assert note and "500x500" in note


def test_a_tiny_request_is_floored_at_the_minimum():
    """Snapping toward zero would emit a `width` the provider rejects outright."""
    width, height, _ = pixels_for(TOGETHER, 8, 8)
    assert (width, height) == (TOGETHER.min_dimension, TOGETHER.min_dimension)


# ── model-level capability holes ─────────────────────────────────────────────


def test_a_negative_prompt_blind_model_still_sends_it_but_discloses():
    """Support is a provider fact, so the field keeps being sent -- a model that
    ignores it today is one the provider may teach it tomorrow. What the user gets
    is the disclosure, at the render that discarded it."""
    built = build_generation_body(TOGETHER, model="black-forest-labs/FLUX.1-schnell", prompt="p", negative_prompt="blurry")
    assert built.body["negative_prompt"] == "blurry"
    assert any("ignores negative prompts" in note for note in built.notes)


def test_a_model_that_honours_the_negative_prompt_is_not_nagged():
    """A note that fires on every render is one users learn to skip."""
    built = build_generation_body(
        TOGETHER, model="stabilityai/stable-diffusion-xl-base-1.0", prompt="p", negative_prompt="blurry"
    )
    assert built.notes == []


def test_no_disclosure_when_there_was_no_negative_prompt_to_drop():
    built = build_generation_body(TOGETHER, model="black-forest-labs/FLUX.1-schnell", prompt="p")
    assert "negative_prompt" not in built.body
    assert built.notes == []


def test_an_overlong_prompt_is_truncated_with_a_note():
    built = build_generation_body(XAI, model="m", prompt="x" * (XAI.max_prompt + 50))
    assert len(built.body["prompt"]) == XAI.max_prompt
    assert any("truncated" in note for note in built.notes)


# ── aspect mapping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "width, height, expected",
    [(1024, 1024, "1:1"), (1920, 1080, "16:9"), (1080, 1920, "9:16"), (1024, 768, "4:3"), (1000, 1500, "2:3")],
)
def test_an_exact_ratio_maps_exactly_and_says_nothing(width, height, expected):
    ratio, note = aspect_for(XAI, width, height)
    assert ratio == expected
    assert note is None


def test_an_inexact_ratio_maps_to_the_nearest_declared_one_and_discloses_it():
    # 1024x1400 is not any declared ratio, and ~7% off is visible in the result.
    ratio, note = aspect_for(XAI, 1024, 1400)
    assert ratio in XAI.aspect_ratios
    assert note and "1024x1400" in note and ratio in note

    # A note on every render is one users learn to skip, which then hides the
    # disclosures that matter -- so 0.6% off square, a few pixels of crop, says nothing.
    assert aspect_for(XAI, 1024, 1030)[1] is None

    # Nearness is measured in log space: a linear metric would call "twice as wide"
    # four times the error of "twice as tall", biasing every off-ratio render.
    assert (aspect_for(XAI, 2000, 1000)[0], aspect_for(XAI, 1000, 2000)[0]) == ("2:1", "1:2")


# ── references ───────────────────────────────────────────────────────────────


def _reference(mime: str = "image/png") -> ResolvedReference:
    return ResolvedReference(
        slot=("cloud", "image_0"),
        source="character",
        data=b"\x89PNG\r\n\x1a\nbytes",
        mime=mime,
        origin="character:card-1",
        digest="d" * 64,
    )


def test_edit_bodies_carry_references_as_data_uris():
    """A data URI means nothing has to be uploaded first, and no third party is
    handed a fetchable URL back into Orb."""
    body = build_edit_body(XAI, model="m", prompt="p", references=[_reference()], width=1024, height=1024).body
    assert body["images"][0]["url"].startswith("data:image/png;base64,")
    assert body["n"] == 1


def test_a_singular_reference_field_discloses_the_ones_it_dropped():
    openai = get_preset("openai")
    assert openai is not None
    built = build_edit_body(openai, model="m", prompt="p", references=[_reference(), _reference()])
    assert isinstance(built.body["image"], dict)
    assert any("one reference image" in note for note in built.notes)


def test_a_string_encoding_carries_the_bare_uri():
    """Together takes `image_url: "data:..."`. Handed the `[{"url": ...}]` shape it
    answers 200 and renders the prompt alone, so the encoding is exactly as
    silently ignorable as the field name -- hence declared, not inferred."""
    body = build_edit_body(TOGETHER, model="black-forest-labs/FLUX.1-kontext-pro", prompt="p", references=[_reference()]).body
    assert body["image_url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    "preset",
    [preset for preset in PRESETS if preset.supports_references],
    ids=[preset.id for preset in PRESETS if preset.supports_references],
)
def test_every_reference_encoding_sends_a_data_uri(preset):
    """Nothing is uploaded first and no third party is handed a fetchable URL back
    into Orb. Verified live on Together: a `data:` URI is accepted."""
    carried = build_edit_body(preset, model="m", prompt="p", references=[_reference()]).body[preset.reference_field]
    if isinstance(carried, str):
        uri = carried
    elif isinstance(carried, list):
        uri = carried[0]["url"]
    else:
        uri = carried["url"]
    assert uri.startswith("data:image/png;base64,"), preset.id


def test_an_allowlist_treats_an_unprobed_model_as_incapable():
    """Under-promising costs a disclosure. Over-promising costs whichever failure
    the provider happens to pick -- Together's FLUX.2 rejects `image_url` outright,
    while its schnell default returns 200 having ignored it. So an unprobed model is
    incapable until someone probes it."""
    assert takes_references(TOGETHER, "black-forest-labs/FLUX.1-kontext-pro") is True
    assert takes_references(TOGETHER, "black-forest-labs/FLUX.1-schnell") is False
    # A provider with no per-model holes names none, and every model passes.
    assert takes_references(XAI, "grok-imagine-image") is True


def test_a_reference_render_discloses_that_it_overrode_the_resolution():
    """Verified on Kontext: a 512x512 reference on a 1024x576 request came back
    1024x1024. The picker still shows a resolution that no longer applies, so the
    render says so rather than leaving it to be noticed."""
    built = build_edit_body(
        TOGETHER,
        model="black-forest-labs/FLUX.1-kontext-pro",
        prompt="p",
        references=[_reference()],
        width=1024,
        height=576,
    )
    assert any("set the output size" in note for note in built.notes)

    # Without a reference there is nothing to override, so nothing is said.
    plain = build_generation_body(TOGETHER, model="black-forest-labs/FLUX.1-kontext-pro", prompt="p", width=1024, height=576)
    assert plain.notes == []


def test_a_provider_whose_references_do_not_drive_the_size_says_nothing():
    built = build_edit_body(XAI, model="m", prompt="p", references=[_reference()], width=1024, height=1024)
    assert not any("set the output size" in note for note in built.notes)


def test_a_provider_without_reference_support_never_takes_them():
    openrouter = get_preset("openrouter")
    assert openrouter is not None
    assert takes_references(openrouter, "anything-at-all") is False
