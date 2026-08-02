"""The preset table and the pure request builders that read it.

The single most important consequence of xAI's wire format: it *silently ignores*
unknown fields, so the API will never tell you a parameter was wrong. A builder
that sends everything and lets the server sort it out is the difference between a
working negative prompt and one the user watches have no effect. Hence the
allowlist, and hence most of the assertions below being about what is **absent**.
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
    provider_catalogue,
)

XAI = get_preset("xai")
assert XAI is not None


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


def test_only_the_probed_provider_claims_to_be_verified():
    """An unverified row is a guess from vendor docs. Saying so in the table is what
    keeps the next person from trusting it as measured fact."""
    assert [preset.id for preset in PRESETS if preset.verified] == ["xai"]


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
    together = get_preset("togetherai")
    assert together is not None
    body = build_generation_body(
        together, model="m", prompt="p", negative_prompt="blurry", seed=7, width=1024, height=1024
    ).body
    assert body["negative_prompt"] == "blurry"
    assert body["seed"] == 7
    assert body["size"] == "1024x1024"


def test_an_overlong_prompt_is_truncated_with_a_note():
    built = build_generation_body(XAI, model="m", prompt="x" * (XAI.max_prompt + 50))
    assert len(built.body["prompt"]) == XAI.max_prompt
    assert any("truncated" in note for note in built.notes)


# ── aspect mapping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "width, height, expected",
    [
        (1024, 1024, "1:1"),
        (1920, 1080, "16:9"),
        (1080, 1920, "9:16"),
        (1024, 768, "4:3"),
        (1000, 1500, "2:3"),
    ],
)
def test_an_exact_ratio_maps_exactly_and_says_nothing(width, height, expected):
    ratio, note = aspect_for(XAI, width, height)
    assert ratio == expected
    assert note is None


def test_an_inexact_ratio_maps_to_the_nearest_and_discloses_it():
    # 1024x1536 is 2:3 exactly; 1024x1400 is not any declared ratio.
    ratio, note = aspect_for(XAI, 1024, 1400)
    assert ratio in XAI.aspect_ratios
    assert note and "1024x1400" in note and ratio in note


def test_nearness_is_measured_in_log_space_so_wide_and_tall_are_symmetric():
    """A linear metric would call "twice as wide" four times the error of "twice as
    tall", and quietly bias every off-ratio render landscape."""
    wide, _ = aspect_for(XAI, 2000, 1000)
    tall, _ = aspect_for(XAI, 1000, 2000)
    assert (wide, tall) == ("2:1", "1:2")


def test_the_note_threshold_is_the_2_percent_it_claims():
    """A note on every render is a note users learn to skip, which then hides the
    disclosures that matter."""
    # 1024x1030 is 0.6% off square -- a few pixels of crop, not worth saying.
    assert aspect_for(XAI, 1024, 1030)[1] is None
    # 1024x1100 is ~7% off, which is visible in the result.
    assert aspect_for(XAI, 1024, 1100)[1] is not None


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
