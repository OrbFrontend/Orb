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

NANOGPT = get_preset("nanogpt")
assert NANOGPT is not None

OPENROUTER = get_preset("openrouter")
assert OPENROUTER is not None

OPENAI = get_preset("openai")
assert OPENAI is not None


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
    assert [preset.id for preset in PRESETS if preset.verified] == [
        "xai",
        "togetherai",
        "openrouter",
        "openai",
        "nanogpt",
    ]


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


def test_the_catalogue_carries_the_whole_dimension_contract():
    """The panel's resolution menu is built from these four, so dropping one from the
    allowlist does not fail here -- it silently goes back to offering sizes that
    `size_for`/`pixels_for` snap to something else, disclosed only after the render is
    billed. Asserted per row, since a default of 0 or () reads the same as absent on
    the wire and only the presence of the key can be checked."""
    for row in provider_catalogue():
        for field in ("sizes", "dimension_mode", "min_dimension", "max_dimension", "dimension_step"):
            assert field in row, f"{row['id']} is missing {field}"
        # Not a menu question but the same one: it decides whether the menu applies at
        # all, and only the panel can say so before the user pays for the answer.
        assert "reference_drives_size" in row, row["id"]


# ── the allowlist ────────────────────────────────────────────────────────────


def _xai_body(**kwargs) -> dict:
    return build_generation_body(XAI, model="grok-imagine-image", prompt="a quiet room", **kwargs).body


def test_xai_never_receives_size_even_though_it_is_the_openai_spelling():
    """xAI rejects it outright ("Argument not supported: size"). That is the polite
    failure; the impolite one is a provider that accepts and ignores it."""
    body = _xai_body(width=1024, height=1024)
    assert "size" not in body
    assert body["aspect_ratio"] == "1:1"


def test_openai_declares_no_response_format_because_it_rejects_the_field():
    """The row's inertness, pinned. Where OpenRouter's allowlist buys *honesty* --
    fields it omits would be silently ignored -- OpenAI's buys the render: every
    undeclared field is answered with HTTP 400 `unknown_parameter`.

    `response_format` is the one that mattered, because it was sent by *preset
    default* rather than by this row, so nothing here looked wrong. `b64_json` comes
    back regardless and `_image_bytes` reads it first, so declaring none loses
    nothing. The absence itself is asserted for every preset in
    `test_no_preset_emits_a_field_it_does_not_declare`.
    """
    assert OPENAI.response_formats == ()
    built = build_generation_body(OPENAI, model="gpt-image-1", prompt="p", quality="high", width=1024, height=1024)
    # The fields it does take, so an empty tuple is not read as "send nothing".
    assert (built.body["size"], built.body["quality"], built.body["n"]) == ("1024x1024", "high", 1)


def test_openai_takes_a_reference_under_its_own_element_key():
    """Verified end to end. `images` is an array like xAI's, and there the agreement
    stops: the element is `{"image_url": "<uri>"}`, and `{"url": ...}` is rejected --
    but only once a *real* model is named, so a probe against a bogus model reads the
    wrong shape as accepted."""
    built = build_edit_body(OPENAI, model="gpt-image-1", prompt="p", references=[_reference()], width=1024, height=1024)
    carried = built.body["images"]
    assert isinstance(carried, list)
    assert list(carried[0]) == ["image_url"]
    assert carried[0]["image_url"].startswith("data:image/png;base64,")
    assert "response_format" not in built.body


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
    if not preset.response_formats:
        # An empty tuple is a contract, not an absent preference: OpenAI answers 400
        # `unknown_parameter` to `response_format` and returns `b64_json` anyway.
        assert "response_format" not in body
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


@pytest.mark.parametrize(
    "model, negative, sent, disclosed",
    [
        # Support is a provider fact, so the field keeps being sent -- a model that
        # ignores it today is one the provider may teach it tomorrow. What the user
        # gets is the disclosure, at the render that discarded it.
        ("black-forest-labs/FLUX.1-schnell", "blurry", True, True),
        # A note that fires on every render is one users learn to skip.
        ("stabilityai/stable-diffusion-xl-base-1.0", "blurry", True, False),
        ("black-forest-labs/FLUX.1-schnell", "", False, False),
    ],
    ids=["blind model discloses", "honouring model is not nagged", "nothing to drop, nothing to say"],
)
def test_a_negative_prompt_is_sent_and_disclosed_per_model(model, negative, sent, disclosed):
    built = build_generation_body(TOGETHER, model=model, prompt="p", negative_prompt=negative)
    assert ("negative_prompt" in built.body) is sent
    assert any("ignores negative prompts" in note for note in built.notes) is disclosed


@pytest.mark.parametrize("preset", [XAI, NANOGPT, OPENAI], ids=["xai 8000", "nanogpt 3000", "openai 32000"])
def test_an_overlong_prompt_is_truncated_to_the_providers_own_limit(preset):
    """NanoGPT is why this is per preset rather than a constant: 3000, verified live,
    and it 400s a longer prompt rather than truncating it. A composed scene plus a
    style prompt clears the default 4000 easily, so truncating here is what keeps
    that from being a failed render.

    OpenAI is the same fact from the other end -- 31,992 characters accepted and
    39,996 rejected, so the 4,000 it inherited from the shared default was dall-e-3's
    limit truncating 28,000 characters this API takes. No dall-e model is in the
    catalogue any more.
    """
    assert (NANOGPT.max_prompt, OPENAI.max_prompt) == (3_000, 32_000)
    built = build_generation_body(preset, model="m", prompt="x" * (preset.max_prompt + 50))
    assert len(built.body["prompt"]) == preset.max_prompt
    assert any("truncated" in note for note in built.notes)


# ── size pass-through ────────────────────────────────────────────────────────


def test_a_size_preset_with_no_menu_sends_the_request_verbatim():
    """A `size` row that declares no menu is taken to accept the request as written,
    rather than being sent nothing. NanoGPT is the row that needs it -- see its
    `dimension_mode` comment for why it has no provider-wide menu to declare."""
    assert NANOGPT.sizes == ()
    built = build_generation_body(NANOGPT, model="cyberrealistic-xl", prompt="p", width=1024, height=576)
    assert built.body["size"] == "1024x576"
    assert built.notes == []


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


def test_a_singular_reference_field_discloses_the_ones_it_dropped():
    custom = get_preset("custom")
    assert custom is not None
    built = build_edit_body(custom, model="m", prompt="p", references=[_reference(), _reference()])
    assert isinstance(built.body["image"], dict)
    assert any("one reference image" in note for note in built.notes)


@pytest.mark.parametrize(
    "preset",
    [preset for preset in PRESETS if preset.supports_references],
    ids=[preset.id for preset in PRESETS if preset.supports_references],
)
def test_every_reference_encoding_sends_a_data_uri(preset):
    """Nothing is uploaded first and no third party is handed a fetchable URL back
    into Orb. The encoding is declared per preset rather than inferred from the field
    name, because getting it wrong is silent: handed the `[{"url": ...}]` shape,
    Together answers 200 and renders the prompt alone."""
    built = build_edit_body(preset, model="m", prompt="p", references=[_reference()], width=1024, height=1024)
    carried = built.body[preset.reference_field]
    if isinstance(carried, str):
        uri = carried
    elif isinstance(carried, list):
        # The element key is the encoding's, not the field's: OpenAI's `images` array
        # wants `image_url` where xAI's wants `url`, and it rejects the other by name.
        (uri,) = carried[0].values()
    else:
        uri = carried["url"]
    assert uri.startswith("data:image/png;base64,"), preset.id
    # An edit body is still one image: `n` is the field that multiplies the bill.
    assert built.body["n"] == 1


def test_an_allowlist_treats_an_unprobed_model_as_incapable():
    """Under-promising costs a disclosure; over-promising costs a paid render that
    quietly drops the reference. So an unprobed model is incapable until probed."""
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


def test_nanogpt_carries_its_reference_as_a_bare_data_uri_under_image():
    """The one spelling NanoGPT rejects is `images: [{"url": ...}]` -- the shape xAI
    and OpenAI want -- which 400s with `missing_image_input`. A bare `data:` URI
    under `image` is what was verified, on both the edits and the generations path."""
    body = build_edit_body(NANOGPT, model="step-image-edit-2", prompt="p", references=[_reference()]).body
    assert isinstance(body["image"], str)
    assert body["image"].startswith("data:image/png;base64,")
    assert "images" not in body


def test_nanogpt_offers_references_on_every_model_and_names_the_trade_once():
    """The row that opts out of the allowlist, so the empty tuple has to keep meaning
    "every model" rather than "none". The trade is stated in `gaps` instead."""
    assert NANOGPT.reference_models == ()
    assert takes_references(NANOGPT, "cyberrealistic-xl") is True
    assert takes_references(NANOGPT, "flux-schnell") is True
    assert any("ignore them" in gap for gap in NANOGPT.gaps)


def test_a_provider_without_reference_support_never_takes_them():
    assert takes_references(OPENROUTER, "anything-at-all") is False


def test_openrouter_sends_neither_a_seed_nor_a_negative_prompt():
    """Both were measured inert, not read off the catalogue -- which advertises
    `seed` on every image model because the images path is a shim over the chat
    schema. Two calls at one seed disagreed on two different model families, so
    emitting the field would make `seed_honored` a claim the user cannot check.

    Unknown fields are accepted silently here, so the allowlist is the only thing
    standing between a dropped field and a user who thinks it applied.
    """
    body = build_generation_body(
        OPENROUTER,
        model="google/gemini-2.5-flash-image",
        prompt="p",
        negative_prompt="blurry, watermark",
        seed=12345,
        quality="high",
        width=1024,
        height=576,
    ).body
    assert "seed" not in body
    assert "negative_prompt" not in body
    assert "quality" not in body
    # The one dimension field it does read, verbatim: the model snaps it itself.
    assert body["size"] == "1024x576"


def test_openrouter_passes_the_requested_size_through_without_a_menu():
    """`size` is honoured, then snapped by the chosen model to its own vocabulary --
    1024x576 came back 1344x768. Declaring a `sizes` menu would snap it a second
    time, to a list the next model does not share, so there is deliberately none and
    no note claiming a substitution Orb did not make."""
    assert OPENROUTER.sizes == ()
    built = build_generation_body(OPENROUTER, model="m", prompt="p", width=1024, height=576)
    assert built.body["size"] == "1024x576"
    assert built.notes == []
