"""Macro expansion in saved image-gen text.

The fields here are authored once and reused in every chat, so the contract is
that `{{char}}` means whoever *this* render is a picture of -- which is what makes
an edit-model instruction like "`<image1>` is `{{char}}`" work at all.
"""

from __future__ import annotations

from backend.core import Macros
from backend.workflows.image_gen import macros as macros_mod
from backend.workflows.image_gen.subjects import Subject


def _macros(char: str = "Iris", user: str = "Chi", cast: str = "") -> Macros:
    return Macros(user=user, char=char, seed="", cast=cast)


def _subject(name: str, appearance: str = "", negative: str = "") -> Subject:
    return Subject(
        member_id=f"member-{name}",
        card_id=f"card-{name}",
        name=name,
        profile={"appearance_prompt": appearance, "negative_prompt": negative},
    )


def test_style_text_resolves_and_machine_fields_stay_literal():
    style = {
        "id": "anime",
        "label": "Anime",
        "prompt_format": "tags",
        "prompt": "masterpiece, {{char}} in frame",
        "negative_prompt": "{{user}} visible",
        "extra_instructions": "<image1> is {{char}}. Never draw {{user}}.",
        "reference_source": "character",
    }
    expanded = macros_mod.expand_style(style, _macros())

    assert expanded["prompt"] == "masterpiece, Iris in frame"
    assert expanded["negative_prompt"] == "Chi visible"
    assert expanded["extra_instructions"] == "<image1> is Iris. Never draw Chi."
    # Identifiers and connection wiring are machine values, not prose.
    assert expanded["id"] == "anime"
    assert expanded["label"] == "Anime"
    assert expanded["prompt_format"] == "tags"
    assert expanded["reference_source"] == "character"
    # The caller's dict is never mutated: the saved config is read again on reroll.
    assert style["prompt"] == "masterpiece, {{char}} in frame"


def test_style_expansion_fires_inline_macros_and_keeps_backticked_text():
    style = {
        "prompt": "{{// internal note }}dusk lighting",
        "extra_instructions": "write `{{char}}` literally",
        "negative_prompt": "",
    }
    expanded = macros_mod.expand_style(style, _macros())

    assert expanded["prompt"] == "dusk lighting"
    assert expanded["extra_instructions"] == "write `{{char}}` literally"
    assert expanded["negative_prompt"] == ""


def test_skill_prose_resolves_while_id_and_label_stay_addressable():
    skills = [
        {
            "id": "over_shoulder",
            "label": "Over {{char}}'s shoulder",
            "description": "Use when {{user}} stands behind {{char}}.",
            "instructions": "Frame from behind {{char}}.",
            "enabled": True,
        }
    ]
    (expanded,) = macros_mod.expand_skills(skills, _macros())

    assert expanded["description"] == "Use when Chi stands behind Iris."
    assert expanded["instructions"] == "Frame from behind Iris."
    # The selector answers with an id and the render discloses the label, so both
    # have to survive the round trip unchanged.
    assert expanded["id"] == "over_shoulder"
    assert expanded["label"] == "Over {{char}}'s shoulder"
    assert expanded["enabled"] is True


def test_each_subject_sheet_resolves_to_its_own_name():
    subjects = (
        _subject("Mara", "{{char}} has short black hair", "{{char}} smiling"),
        _subject("Ren", "{{char}} wears a blue jacket"),
    )
    # A group chat's {{char}} is the scene title; a sheet saved on a card is not
    # about the scene.
    mara, ren = macros_mod.expand_subjects(subjects, _macros(char="A quiet room", cast="Mara, Ren"))

    assert mara.profile["appearance_prompt"] == "Mara has short black hair"
    assert mara.profile["negative_prompt"] == "Mara smiling"
    assert ren.profile["appearance_prompt"] == "Ren wears a blue jacket"
    # Everything else about the subject survives the rewrite.
    assert (ren.member_id, ren.card_id, ren.name) == ("member-Ren", "card-Ren", "Ren")


def test_subject_scoping_leaves_the_other_identities_alone():
    scoped = macros_mod.for_subject(_macros(char="A quiet room", user="Chi", cast="Mara, Ren"), "Mara")

    assert (scoped.char, scoped.user, scoped.cast) == ("Mara", "Chi", "Mara, Ren")


def test_an_unnamed_subject_keeps_the_conversation_identity():
    (subject,) = macros_mod.expand_subjects((_subject("", "{{char}} in the doorway"),), _macros(char="Iris"))

    assert subject.profile["appearance_prompt"] == "Iris in the doorway"
