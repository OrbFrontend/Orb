"""Group character-context modes: what each mode puts where, and what it must never.

The three modes redistribute the *same* fields between the shared cached system
body and the speaker's trailing Writer message. These tests pin both halves at
once, because a field that silently appears in both is billed twice and a field
that appears in neither is silently lost.
"""

from __future__ import annotations

import pytest

from backend.core import CastMember, Macros, TurnCast
from backend.pipeline.passes.writer import SHEET_FRAMING, build_writer_content
from backend.prompting import build_prefix
from backend.prompting.group_context import (
    context_size_components,
    render_active_card,
    render_cast_section,
)

MODES = ("private", "shared", "swap")


def _member(mid: str, name: str, **fields) -> CastMember:
    scene = fields.get("scene", "")
    return CastMember(
        mid,
        name.casefold(),
        mid,
        name,
        fields.get("kind", "character"),
        # Mirrors database.queries.group_members._public_profile: a scene
        # override *replaces* the card's profile rather than sitting beside it,
        # so a hand-built member can't disagree with one resolve_cast would
        # produce.
        scene or fields.get("public", ""),
        fields.get("private", ""),
        fields.get("example", ""),
        fields.get("post_history", ""),
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
        "",
        "",
        False,
        "What happened?",
        [],
        None,
        speaker=speaker,
        macros=Macros("User", "Scene", cast="Aria, Kael"),
        context_mode=mode,
        **kwargs,
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


def test_only_private_frames_the_speakers_sheet_against_the_transcript():
    """Private is the one mode that reads a speaker's own sheet *after* history,
    so it is the one mode that has to say the transcript outranks it. Shared and
    Swap put the same text in the system body *before* history, where the
    ordinary reading order already does that work and where the line would be
    billed to the whole cast for nothing."""
    assert SHEET_FRAMING in _tail("private", ARIA)
    for mode in ("shared", "swap"):
        assert SHEET_FRAMING not in _tail(mode, ARIA)


def test_the_sheet_framing_never_ships_without_a_sheet_to_frame():
    """A narrator with no card text would otherwise get a caveat about a
    reference sheet the prompt never shows it."""
    assert SHEET_FRAMING not in _tail("private", _member("n", "Narrator"))


def test_private_prefix_ignores_the_speaker_entirely():
    """The public cast is the same for everyone, so one base serves the exchange."""
    assert _system("private", ARIA) == _system("private", KAEL) == _system("private", None)


@pytest.mark.parametrize("mode", ["private", "swap"])
def test_both_boundary_modes_carry_the_scene_override(mode):
    """Private and Swap each keep a card away from everyone but its owner, so in
    both the curated view is the only thing the rest of the cast is told about a
    member — and in both the override replaces the card profile rather than
    sitting beside it. Shared is the mode that drops it."""
    system = _system(mode, ARIA)
    assert "### Aria\nARIA SCENE OVERRIDE" in system
    assert "### Kael\nRole: mage" in system
    assert "ARIA SCENE OVERRIDE" not in _system("shared", ARIA)


def test_the_two_boundary_modes_render_one_and_the_same_public_cast():
    """The consolidation invariant. Private and Swap differ only in where the
    speaker's own card sits; what the cast is told about everyone is one block
    from one renderer, so a change to either mode's visibility is a change to
    both or it is a bug."""
    public = render_cast_section(_cast("private", ARIA), MACROS)
    swap = render_cast_section(_cast("swap", ARIA), MACROS)
    assert swap.startswith(public)
    # Everything Swap adds past that block is the speaker's own card, nothing else.
    assert swap[len(public) :] == render_active_card(ARIA, MACROS, "Aria, Kael")


# ── Shared dossier ──────────────────────────────────────────────────────────


def test_shared_publishes_one_dossier_per_member_and_never_repeats_it_in_the_tail():
    system = _system("shared", ARIA)
    assert system.count("## Character dossier: Aria") == 1
    assert system.count("## Character dossier: Kael") == 1
    for shared_fact in ("ARIA SHEET", "KAEL SHEET", "ARIA EXAMPLE", "KAEL EXAMPLE"):
        assert shared_fact in system
    # Card text only. The override is a Private-perspective instrument.
    assert "ARIA SCENE OVERRIDE" not in system

    tail = _tail("shared", ARIA)
    assert "## You are writing as Aria" in tail
    assert "ARIA SHEET" not in tail and "ARIA EXAMPLE" not in tail
    assert "Write the next reply as Aria only" in tail


def test_shared_never_layers_a_curated_profile_over_the_cards_it_already_shares():
    """Every member reads every other member's card here, so a curated profile
    would be a second view of the same member — and it rendered as a label on
    labels (`Public profile: Appearance: …`). Neither provenance survives."""
    system = _system("shared", ARIA)
    assert "ARIA SCENE OVERRIDE" not in system  # the Manage cast override
    assert "Role: mage" not in system  # the card's own public profile
    assert "Scene profile:" not in system and "Public profile:" not in system


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


def test_swap_sends_only_the_active_card_and_public_profiles_for_everyone_else():
    system = _system("swap", ARIA)
    # The public cast rides the system prompt exactly as it does under Private…
    assert "### Aria\nARIA SCENE OVERRIDE" in system and "### Kael\nRole: mage" in system
    # …and the speaker's own card is appended after it, card-style.
    assert "## Character: Aria" in system and "ARIA SHEET" in system and "ARIA EXAMPLE" in system
    assert system.index("### Kael") < system.index("## Character: Aria")
    # No other member's card text, in either half.
    for hidden in ("KAEL SHEET", "KAEL EXAMPLE"):
        assert hidden not in system
    tail = _tail("swap", ARIA)
    assert "ARIA SHEET" not in tail and "ARIA EXAMPLE" not in tail
    assert "ARIA DIRECTIVE" in tail


def test_swap_names_a_member_that_has_no_profile_to_show():
    """The names-only floor the old cast list provided: every active member is
    named whether or not anyone wrote a profile for it."""
    narrator = _member("n", "Narrator", kind="narrator")
    system = str(
        build_prefix("system", "", "", macros=MACROS, cast=TurnCast(True, (ARIA, narrator), ARIA, "swap"))[0]["content"]
    )
    assert "### Narrator" in system
    assert "## Character: Narrator" not in system


def test_swap_without_a_speaker_is_the_neutral_base_every_speaker_extends():
    """The Director runs before the plan exists, so it must never see a card.

    It does see the public cast: that block is written without reference to the
    speaker, which is precisely what keeps the neutral base a byte-prefix of
    every speaker's rather than a fourth variant.
    """
    neutral = _system("swap", None)
    public = render_cast_section(_cast("swap"), MACROS)
    assert "### Aria\nARIA SCENE OVERRIDE" in neutral and "### Kael\nRole: mage" in neutral
    assert "ARIA SHEET" not in neutral and "KAEL SHEET" not in neutral
    for speaker in (ARIA, KAEL):
        scoped = _system("swap", speaker)
        assert scoped != neutral
        # Everything up to the active card is shared with the neutral base.
        assert scoped.startswith(neutral[: neutral.index(public) + len(public)])


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
        "", "", False, "Go on", [], None, speaker=gambler, macros=seeded, context_mode="private"
    )
    assert isinstance(in_private_tail, str)

    rolled = in_shared_body.split("Bet ")[1].split(" on it")[0]
    assert rolled.isdigit(), in_shared_body
    assert f"Bet {rolled} on it" in in_private_tail
    # And it is stable turn over turn, which is the point of seeding at all.
    assert in_private_tail == build_writer_content(
        "", "", False, "Go on", [], None, speaker=gambler, macros=seeded, context_mode="private"
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
    # Private and Swap share a key because they measure the same block.
    assert keys["swap"] == ["cast_public", "largest_active_card", "largest_speaker_tail"]


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


@pytest.mark.parametrize("mode", MODES)
def test_the_scenes_own_directive_reaches_every_mode_and_a_cards_never_reaches_the_body(mode):
    """`conversations.post_history_instructions` is the scene's, not a card's.

    There is exactly one per scene and it is the same for every speaker, so it
    belongs in the shared cached body where a solo chat already carries it.
    Suppressing it for groups made the "How should this scene be written?" box in
    both group modals a field that persisted and was never sent.

    A *card's* directive keeps the opposite rule: active-only, in the tail, because
    merging several of them produces contradictory control instructions.
    """
    speaker = _member("a", "Aria", post_history="CARD DIRECTIVE")
    system = _system(mode, speaker, post_history_instructions="SCENE DIRECTIVE")
    assert "SCENE DIRECTIVE" in system
    assert "CARD DIRECTIVE" not in system

    tail = _tail(mode, speaker)
    assert "CARD DIRECTIVE" in tail
    assert "SCENE DIRECTIVE" not in tail
