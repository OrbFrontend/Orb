"""Group character-context modes: what each mode puts where, and what it must never.

The three modes redistribute the *same* fields between the shared cached system
body and the speaker's trailing Writer message. These tests pin both halves at
once, because a field that silently appears in both is billed twice and a field
that appears in neither is silently lost.
"""

from __future__ import annotations

import pytest

from backend.core import CastMember, Macros, TurnCast
from backend.inference.group_context import context_size_components, render_cast_section
from backend.inference.prompt_builder import build_prefix
from backend.pipeline.passes.writer import build_writer_content

MODES = ("private", "shared", "swap")


def _member(mid: str, name: str, **fields) -> CastMember:
    scene = fields.get("scene", "")
    return CastMember(
        mid,
        name.casefold(),
        mid,
        name,
        fields.get("kind", "character"),
        # Mirrors pipeline.cast._public_profile: a scene override *replaces* the
        # card's profile rather than sitting beside it, so a hand-built member
        # can't disagree with one resolve_cast would produce.
        scene or fields.get("public", ""),
        fields.get("private", ""),
        fields.get("example", ""),
        fields.get("post_history", ""),
        scene,
        fields.get("muted", False),
    )


ARIA = _member(
    "a",
    "Aria",
    public="Role: scout",
    private="ARIA SHEET, and {{char}} keeps watch",
    example="ARIA EXAMPLE",
    post_history="ARIA DIRECTIVE",
    scene="ARIA SCENE OVERRIDE",
)
KAEL = _member("k", "Kael", public="Role: mage", private="KAEL SHEET", example="KAEL EXAMPLE", post_history="KAEL DIRECTIVE")

MACROS = Macros("User", "Campfire", seed="conv-1", cast="Aria, Kael")


def _cast(mode: str, speaker: CastMember | None = None) -> TurnCast:
    return TurnCast(True, (ARIA, KAEL), speaker, mode)


def _system(mode: str, speaker: CastMember | None = None, **kwargs) -> str:
    prefix = build_prefix(
        "system",
        "legacy persona",
        "At {{char}}'s camp with {{cast}}.",
        macros=MACROS,
        cast=_cast(mode, speaker),
        **kwargs,
    )
    return str(prefix[0]["content"])


def _tail(mode: str, speaker: CastMember, **kwargs) -> str:
    content = build_writer_content(
        "", "", False, "What happened?", [], None, speaker=speaker, cast_names="Aria, Kael", context_mode=mode, **kwargs
    )
    assert isinstance(content, str)
    return content


# ── Private perspective — the default, and behaviour-preserving ──────────────


def test_private_keeps_other_members_raw_cards_out_of_a_speakers_whole_prompt():
    system = _system("private", ARIA)
    tail = _tail("private", ARIA)
    # Aria is shown through her scene override, Kael through his card profile.
    assert "## Cast" in system and "ARIA SCENE OVERRIDE" in system and "Role: mage" in system
    for hidden in ("ARIA SHEET", "KAEL SHEET", "ARIA EXAMPLE", "KAEL EXAMPLE", "KAEL DIRECTIVE"):
        assert hidden not in system
    # Aria's own card reaches only Aria's own trailing message.
    assert "ARIA SHEET" in tail and "ARIA EXAMPLE" in tail and "ARIA DIRECTIVE" in tail
    assert "KAEL SHEET" not in tail and "KAEL DIRECTIVE" not in tail


def test_private_prefix_ignores_the_speaker_entirely():
    """The public cast is the same for everyone, so one base serves the beat."""
    assert _system("private", ARIA) == _system("private", KAEL) == _system("private", None)


# ── Shared dossier ──────────────────────────────────────────────────────────


def test_shared_publishes_one_dossier_per_member_and_never_repeats_it_in_the_tail():
    system = _system("shared", ARIA)
    assert system.count("## Character dossier: Aria") == 1
    assert system.count("## Character dossier: Kael") == 1
    for shared_fact in ("ARIA SHEET", "KAEL SHEET", "ARIA EXAMPLE", "KAEL EXAMPLE"):
        assert shared_fact in system
    # A user-authored scene override is framing, not a redaction: it rides
    # inside its own member's dossier rather than being dropped.
    assert "Scene profile: ARIA SCENE OVERRIDE" in system

    tail = _tail("shared", ARIA)
    assert "## You are writing as Aria" in tail
    assert "ARIA SHEET" not in tail and "ARIA EXAMPLE" not in tail
    assert "Write the next reply as Aria only" in tail


def test_shared_ships_the_curated_profile_labelled_by_where_it_came_from():
    """Nothing is hidden in this mode, so authored framing must not be dropped —
    and the reader has to be able to tell scene-local framing from card text."""
    system = _system("shared", ARIA)
    assert "Scene profile: ARIA SCENE OVERRIDE" in system  # Manage cast override
    assert "Public profile: Role: mage" in system  # the card's own profile


def test_shared_still_publishes_a_member_whose_card_is_all_profile():
    """A card carrying a public profile but no description would otherwise
    render an empty dossier and survive as a bare name — switching to the mode
    that shares *more* would show less about it than Private did."""
    facade = _member("f", "Facade", public="Role: the innkeeper")
    system = str(
        build_prefix("system", "", "", macros=MACROS, cast=TurnCast(True, (ARIA, facade), ARIA, "shared"))[0]["content"]
    )
    assert "## Character dossier: Facade" in system
    assert "Public profile: Role: the innkeeper" in system


def test_shared_keeps_post_history_directives_active_only():
    """Concatenating N post-history blocks would produce N competing directives."""
    system = _system("shared", ARIA)
    assert "ARIA DIRECTIVE" not in system and "KAEL DIRECTIVE" not in system
    assert "ARIA DIRECTIVE" in _tail("shared", ARIA)
    assert "ARIA DIRECTIVE" not in _tail("shared", KAEL)


def test_shared_dossiers_follow_the_active_roster_order_a_muted_member_included():
    """A muted member is in scene and never speaks — it still has to be known."""
    system = _system("shared", ARIA)
    assert system.index("dossier: Aria") < system.index("dossier: Kael")
    # resolve_cast hands over the active roster in `sort_order, id`; muting is a
    # turn-policy flag it deliberately does not filter on.
    assert system.count("## Character dossier: ") == 2


def test_shared_skips_a_member_with_nothing_to_say_but_still_names_it():
    narrator = _member("n", "Narrator", kind="narrator")
    system = str(
        build_prefix("system", "", "", macros=MACROS, cast=TurnCast(True, (ARIA, narrator), ARIA, "shared"))[0]["content"]
    )
    assert "## Character dossier: Narrator" not in system
    assert "## Cast\nAria, Narrator" in system


# ── Classic card swap ───────────────────────────────────────────────────────


def test_swap_sends_only_the_active_card_and_names_for_everyone_else():
    system = _system("swap", ARIA)
    assert "## Cast\nAria, Kael" in system
    assert "## Character: Aria" in system and "ARIA SHEET" in system and "ARIA EXAMPLE" in system
    for hidden in ("KAEL SHEET", "KAEL EXAMPLE", "Role: mage", "ARIA SCENE OVERRIDE"):
        assert hidden not in system
    tail = _tail("swap", ARIA)
    assert "ARIA SHEET" not in tail and "ARIA EXAMPLE" not in tail
    assert "ARIA DIRECTIVE" in tail


def test_swap_without_a_speaker_is_the_neutral_base_every_speaker_extends():
    """The Director runs before the plan exists, so it must never see a card."""
    neutral = _system("swap", None)
    assert "## Cast\nAria, Kael" in neutral
    assert "ARIA SHEET" not in neutral and "KAEL SHEET" not in neutral
    for speaker in (ARIA, KAEL):
        scoped = _system("swap", speaker)
        assert scoped != neutral
        # Everything up to the active card is shared with the neutral base.
        assert scoped.startswith(neutral[: neutral.index("## Cast") + len("## Cast\nAria, Kael")])


# ── Macro scoping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["shared", "swap"])
def test_card_text_moved_into_the_shared_body_resolves_char_to_its_own_member(mode):
    system = _system(mode, ARIA)
    assert "ARIA SHEET, and Aria keeps watch" in system
    # Everything outside a dossier / active card keeps group-title scoping.
    assert "At Campfire's camp with Aria, Kael." in system


def test_private_leaves_char_scoped_to_the_group_outside_the_speaker_tail():
    assert "At Campfire's camp with Aria, Kael." in _system("private", ARIA)
    assert "ARIA SHEET, and Aria keeps watch" in _tail("private", ARIA)


def test_the_same_card_rolls_the_same_wherever_the_mode_routes_it():
    """A card's inline macros resolve under the conversation's macro_seed in
    every mode. Without the seed on the tail, Private would re-roll every turn
    (busting its own bytes) and disagree with the value Shared bakes into the
    cached body for the identical card."""
    gambler = _member("g", "Gambler", private="Bet {{roll::1d20}} on it")
    seeded = Macros("User", "Campfire", seed="conv-1", cast="Gambler")

    in_shared_body = render_cast_section(TurnCast(True, (gambler,), gambler, "shared"), seeded)
    in_private_tail = build_writer_content(
        "", "", False, "Go on", [], None, speaker=gambler, cast_names="Gambler", macro_seed="conv-1", context_mode="private"
    )
    assert isinstance(in_private_tail, str)

    rolled = in_shared_body.split("Bet ")[1].split(" on it")[0]
    assert rolled.isdigit(), in_shared_body
    assert f"Bet {rolled} on it" in in_private_tail
    # And it is stable turn over turn, which is the point of seeding at all.
    assert in_private_tail == build_writer_content(
        "", "", False, "Go on", [], None, speaker=gambler, cast_names="Gambler", macro_seed="conv-1", context_mode="private"
    )


# ── Cross-mode invariants ───────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_keeps_the_speaker_only_guard_and_the_scene_premise(mode):
    assert "Write the next reply as Aria only" in _tail(mode, ARIA)
    assert "## Scenario\nAt Campfire's camp with Aria, Kael." in _system(mode, ARIA)


@pytest.mark.parametrize("mode", MODES)
def test_prevent_prompt_overrides_still_suppresses_post_history_in_every_mode(mode):
    assert "ARIA DIRECTIVE" not in _tail(mode, ARIA, prevent_prompt_overrides=True)


@pytest.mark.parametrize("mode", MODES)
def test_no_mode_reads_a_card_system_prompt_or_scenario(mode):
    """Swap substitutes identity, never control instructions — see group_context.py."""
    system = _system(mode, ARIA)
    assert "legacy persona" not in system


def test_a_solo_turn_renders_no_cast_section_whatever_the_mode_says():
    solo = TurnCast(False, (ARIA,), ARIA, "swap")
    assert render_cast_section(solo, MACROS) == ""


# ── Context-size components ─────────────────────────────────────────────────


def test_size_components_name_what_each_mode_actually_bills():
    keys = {mode: [key for key, _ in context_size_components(_cast(mode), MACROS)] for mode in MODES}
    assert keys["private"] == ["cast_public", "largest_speaker_tail"]
    assert keys["shared"] == ["cast_dossiers", "largest_speaker_tail"]
    assert keys["swap"] == ["cast_names", "largest_active_card", "largest_speaker_tail"]


def test_size_components_measure_the_same_text_the_prompt_sends():
    for mode in MODES:
        components = dict(context_size_components(_cast(mode), MACROS, prevent_prompt_overrides=False))
        shared_key = next(key for key in components if key.startswith("cast_"))
        assert components[shared_key] == render_cast_section(_cast(mode), MACROS)
        # Only Private bills identity fields per speaker; the others moved them
        # into the shared body, so the tail is directives alone.
        tail = components["largest_speaker_tail"]
        assert ("ARIA SHEET" in tail) is (mode == "private")
        assert "KAEL DIRECTIVE" in tail or "ARIA DIRECTIVE" in tail


def test_size_components_never_bill_a_member_that_cannot_take_the_turn():
    """A muted member is in scene — it keeps its dossier — but it is never
    scheduled, so a maximum built from its card would overstate every call."""
    loud = _member("l", "Loud", private="X" * 400, muted=True)
    quiet = _member("q", "Quiet", private="Y" * 10)
    for mode, key in (("private", "largest_speaker_tail"), ("swap", "largest_active_card")):
        components = dict(context_size_components(TurnCast(True, (loud, quiet), None, mode), MACROS))
        assert "X" * 400 not in components[key]
        assert "Y" * 10 in components[key]
    # Muted or not, it is still part of the scene the cast is told about.
    assert "X" * 400 in dict(context_size_components(TurnCast(True, (loud, quiet), None, "shared"), MACROS))["cast_dossiers"]


def test_size_components_survive_an_empty_roster_and_cardless_members():
    empty = TurnCast(True, (), None, "shared")
    assert [text for _, text in context_size_components(empty, MACROS)] == ["\n\n## Cast\n", ""]
    narrator = TurnCast(True, (_member("n", "Narrator", kind="narrator"),), None, "swap")
    assert dict(context_size_components(narrator, MACROS))["largest_active_card"] == "\n\n## Character: Narrator"
