"""A group render carries fewer likenesses than it has people, and must say so.

The failure this guards is the quiet one. Four members are in frame, the target
carries two reference images, and the render is *correct* -- the composer describes
the two who got no picture instead of suppressing their identity traits -- but two
of them come back looking like strangers with nothing on screen to explain it. The
render already knew; it just never said.

Only a **mixed** render says anything. Nobody pictured is `_unfilled_note`'s to
describe, or is a style pointed at the chat image on purpose.
"""

from __future__ import annotations

import pytest

from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.engine import get_adapter
from backend.workflows.image_gen.engine.contracts import ResolvedReference
from backend.workflows.image_gen.hooks import _uncovered_note
from backend.workflows.image_gen.subjects import Subject


def _subject(name: str, card_id: str | None = "") -> Subject:
    return Subject(member_id=f"m-{name}", card_id=card_id if card_id != "" else f"card-{name}", name=name)


def _reference(card_id: str, index: int = 0) -> ResolvedReference:
    """A likeness that went out for one card, as `resolve_references` records it."""
    return ResolvedReference(
        slot=("images", f"image_{index}"),
        source="cast",
        data=b"PNG",
        mime="image/png",
        origin=f"character:{card_id}",
        digest=f"d{index}",
    )


# ── the reported case ────────────────────────────────────────────────────────


def test_a_four_hander_on_a_two_slot_target_names_who_was_only_described():
    """The case that prompted this: the cast outruns the provider's array."""
    cast = [_subject("Kael"), _subject("Mara"), _subject("Sera"), _subject("Tovin")]
    sent = [_reference("card-Kael", 0), _reference("card-Mara", 1)]

    note = _uncovered_note(cast, sent, declared=2, capacity=2)

    assert "Sera and Tovin were described in the prompt rather than pictured" in note
    assert "this render carries 2 reference images" in note
    # The two who *were* pictured are not named as losses.
    assert "Kael" not in note and "Mara" not in note


def test_a_style_below_its_target_capacity_points_at_the_style_instead():
    """Same outcome, different remedy: rows the user can still switch on."""
    cast = [_subject("Kael"), _subject("Mara")]
    note = _uncovered_note(cast, [_reference("card-Kael")], declared=1, capacity=4)

    assert "Mara was described in the prompt rather than pictured" in note
    assert "this style fills 1 of its 4 reference slots" in note
    assert "carries" not in note


def test_one_uncovered_member_reads_as_singular():
    note = _uncovered_note([_subject("Kael"), _subject("Mara")], [_reference("card-Kael")], declared=1, capacity=1)
    assert "Mara was described" in note
    assert "carries 1 reference image" in note and "images" not in note


# ── when it must stay quiet ──────────────────────────────────────────────────


def test_a_render_that_pictured_nobody_says_nothing_here():
    """`_unfilled_note` owns that story, and a style pointed at the chat image is a
    setting rather than a loss. Two notes for one fact teaches users to skip both."""
    cast = [_subject("Kael"), _subject("Mara")]
    assert _uncovered_note(cast, [], declared=1, capacity=2) == ""


def test_a_render_that_pictured_everybody_says_nothing():
    cast = [_subject("Kael"), _subject("Mara")]
    sent = [_reference("card-Kael", 0), _reference("card-Mara", 1)]
    assert _uncovered_note(cast, sent, declared=2, capacity=2) == ""


def test_a_solo_render_says_nothing():
    assert _uncovered_note([_subject("Kael")], [_reference("card-Kael")], declared=1, capacity=1) == ""


def test_a_previous_image_reference_names_nobody_and_so_stays_quiet():
    """A `previous` slot records no `character:` origin, so nobody is covered -- which
    is "no likenesses", not "some likenesses and some losses"."""
    chat_image = ResolvedReference(
        slot=("images", "image_0"),
        source="previous",
        data=b"PNG",
        mime="image/png",
        origin="attachment:7",
        digest="d",
    )
    cast = [_subject("Kael"), _subject("Mara")]
    assert _uncovered_note(cast, [chat_image], declared=1, capacity=1) == ""


# ── who counts as denied a slot ──────────────────────────────────────────────


def test_a_subject_with_no_card_was_never_denied_a_slot():
    """A narrator member, or one whose card was deleted, is addressable in the prompt
    and can never fill a slot. Naming them would report a loss nothing could fix."""
    cast = [_subject("Kael"), _subject("Narrator", card_id=None), _subject("Mara")]
    note = _uncovered_note(cast, [_reference("card-Kael")], declared=1, capacity=1)

    assert "Mara was described" in note
    assert "Narrator" not in note


def test_a_member_the_analyzer_left_out_of_frame_is_not_a_loss():
    """The caller passes `addressable_subjects`' answer, not the full cast: somebody
    out of frame contributes nothing to the prompt either, so the render took no loss
    on them. Pinned by passing the filtered list this function is contracted to get."""
    in_frame = [_subject("Kael"), _subject("Mara")]
    note = _uncovered_note(in_frame, [_reference("card-Kael")], declared=1, capacity=1)
    assert "Sera" not in note


def test_the_name_list_is_bounded_for_a_crowd():
    """A twelve-hander must not print twelve names into one disclosure line."""
    cast = [_subject("Kael")] + [_subject(f"Extra{n}") for n in range(6)]
    note = _uncovered_note(cast, [_reference("card-Kael")], declared=1, capacity=1)

    assert "Extra0, Extra1, Extra2 and 3 others were described" in note
    assert "Extra5" not in note


def test_two_slots_on_one_card_do_not_count_that_card_twice():
    """Two `character` rows send one person's likeness twice. They are one covered
    subject, not two, and must not make a second member look covered."""
    cast = [_subject("Kael"), _subject("Mara")]
    twice = [_reference("card-Kael", 0), _reference("card-Kael", 1)]
    note = _uncovered_note(cast, twice, declared=2, capacity=2)
    assert "Mara was described" in note


# ── the targets publish the ceiling ──────────────────────────────────────────


def _cloud_config(provider: str, model: str, sources: list[str]) -> dict:
    return normalize_config(
        {
            "source": "cloud",
            "default_style": "s",
            "styles": [{"id": "s", "label": "S", "connection": provider, "model": model, "reference_sources": sources}],
            "cloud": {"provider": provider, "providers": {provider: {"api_key": "k"}}},
        }
    )


def _target(config: dict):
    return get_adapter(config, resolve_style(config, "s")).resolve_target(None)


def test_a_cloud_target_publishes_the_provider_ceiling_not_the_style_row_count():
    """The ceiling is what says whether an uncovered member is the style's doing."""
    target = _target(_cloud_config("xai", "grok-imagine-image", ["character"]))
    assert target.reference_capacity == 4
    assert len(target.reference_slots) == 1


def test_a_cloud_target_that_cannot_carry_a_reference_publishes_zero():
    """OpenRouter has no reference field on this path at all -- measured across three
    spellings -- so there is nothing to send and no ceiling to report. A zero can never
    read as "the style left a row Off", which would send the user to a setting that
    would not help.

    Provider-level, and deliberately not asked of the model: a *model* that will not
    take a reference refuses at render time and the seam degrades, where a withheld
    slot would have lost the capability silently."""
    config = _cloud_config("openrouter", "google/gemini-2.5-flash-image", ["character"])
    target = _target(config)
    assert target.reference_slots == ()
    assert target.reference_capacity == 0


def test_a_comfy_target_publishes_the_graph_declaration_not_what_the_style_switched_on():
    """How many inputs load an image is structural and found at import; which of them
    a style points at a source has been editable since."""
    graph = {
        "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "a": {"class_type": "LoadImage", "inputs": {"image": "a.png"}},
        "b": {"class_type": "LoadImage", "inputs": {"image": "b.png"}},
        "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
    }
    config = normalize_config(
        {
            "source": "external_comfy",
            "default_style": "s",
            "styles": [
                {"id": "s", "label": "S", "connection": "comfy", "workflow": "g", "reference_sources": ["character", ""]}
            ],
            "external_comfy": {
                "user_graphs": [
                    {
                        "id": "g",
                        "label": "G",
                        "graph": graph,
                        "slots": {
                            "positive": ["0", "text"],
                            "seed": ["s", "seed"],
                            "output": ["o", "images"],
                            "references": [{"slot": ["a", "image"]}, {"slot": ["b", "image"]}],
                        },
                    }
                ]
            },
        }
    )
    target = _target(config)
    assert target.reference_capacity == 2
    assert len(target.reference_slots) == 1


@pytest.mark.parametrize("sources", [[], ["", ""]])
def test_capacity_survives_a_style_with_every_row_off(sources):
    """Capacity is a fact about the target, not about what the style switched on --
    otherwise "0 of 0 slots" would be the answer for a style that has simply not been
    configured yet."""
    target = _target(_cloud_config("xai", "grok-imagine-image", sources))
    assert target.reference_slots == ()
    assert target.reference_capacity == 4
