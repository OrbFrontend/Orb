from __future__ import annotations

from backend.core import CastMember, Macros, TurnCast
from backend.database.queries.group_members import allocate_speaker_key
from backend.pipeline.cast import parse_speaking_plan, plan_cue, round_robin_member
from backend.pipeline.passes.director import (
    build_direct_scene_override,
    speaking_plan_instruction,
)
from backend.pipeline.passes.writer import build_writer_content, strip_speaker_label
from backend.pipeline.state import _DIRECTOR_SEED_FIELDS, TurnState
from backend.prompting import build_prefix


def _member(mid: str, name: str, public: str, private: str) -> CastMember:
    return CastMember(mid, name.casefold(), mid, name, "character", public, private, "", "")


def test_group_base_contains_only_public_profiles_and_labelled_history():
    aria = _member("a", "Aria", "Role: scout", "ARIA PRIVATE")
    kael = _member("k", "Kael", "Role: mage", "KAEL PRIVATE")
    cast = TurnCast(True, (aria, kael))
    prefix = build_prefix(
        "system",
        "legacy private",
        "At {{char}}'s camp with {{cast}}.",
        messages=[
            {"role": "assistant", "content": "First", "speaker_member_id": "a"},
            {"role": "assistant", "content": "Second", "speaker_member_id": "k"},
        ],
        macros=Macros("User", "Campfire", cast="Aria, Kael"),
        cast=cast,
    )
    system = prefix[0]["content"]
    assert "## Cast" in system and "Role: scout" in system and "Role: mage" in system
    assert "ARIA PRIVATE" not in system and "KAEL PRIVATE" not in system and "legacy private" not in system
    assert "At Campfire's camp with Aria, Kael." in system
    assert prefix[1] == {"role": "assistant", "content": "Aria: First\n\nKael: Second"}


def test_speaker_private_sheet_is_only_in_speaker_tail_and_empty_message_has_no_separator():
    aria = _member("a", "Aria", "Role: scout", "ARIA PRIVATE {{char}} {{cast}}")
    content = build_writer_content(
        "",
        "",
        False,
        "",
        [],
        None,
        speaker=aria,
        speaker_cue="Take watch",
        macros=Macros("User", "Campfire", cast="Aria, Kael"),
    )
    assert isinstance(content, str)
    assert "ARIA PRIVATE Aria Aria, Kael" in content
    assert "## Your cue\nTake watch" in content
    assert "Write the next reply as Aria only" in content
    assert "___\n\n\n\n" not in content


def test_speaker_label_stripper_uses_the_complete_escaped_name():
    name = "A Very Long [Speaker] Name That Exceeds Forty Eight Characters Exactly"
    body = "The lamps flicker awake."
    assert strip_speaker_label(f"**{name}:**\n{body}", name) == body
    assert strip_speaker_label(f"### {name}\n{body}", name) == body
    assert strip_speaker_label(f"{name}: {body}", name) == body
    assert strip_speaker_label(f"Another: {body}", name) == f"Another: {body}"


def test_speaker_label_stripper_leaves_the_name_in_prose_alone():
    """A colon makes a label; bold alone does not.

    ``**Aria** crosses the room.`` is a sentence that opens on the character's
    name, which is how a great many cards write. Treating the bold as a label
    deleted the subject of the sentence.
    """
    assert strip_speaker_label("**Aria** crosses the room.", "Aria") == "**Aria** crosses the room."
    assert strip_speaker_label("__Aria__ crosses the room.", "Aria") == "__Aria__ crosses the room."
    assert strip_speaker_label("Aria crosses the room.", "Aria") == "Aria crosses the room."
    # Still a label when a colon says so, or when the name owns its whole line.
    assert strip_speaker_label("**Aria**: hello", "Aria") == "hello"
    assert strip_speaker_label("**Aria**\nShe crosses the room.", "Aria") == "She crosses the room."


def test_group_director_schema_and_plan_policy_distinguish_rest_from_malformed():
    members = [
        {"id": "a", "speaker_key": "aria", "display_name": "Aria", "active": 1, "muted": 0},
        {"id": "k", "speaker_key": "kael", "display_name": "Kael", "active": 1, "muted": 0},
        {"id": "m", "speaker_key": "mira", "display_name": "Mira", "active": 1, "muted": 1},
    ]
    schema = build_direct_scene_override([], grouped=True)
    prop = schema["function"]["parameters"]["properties"]["speaking_plan"]
    # The blob is the cached prefix (kv-cache.md, Invariant 3), so it names the
    # field and never the cast: a mute toggle changes nothing here, and the live
    # roster is stated on the Director's trailing request instead.
    assert "speaker_key" in prop["description"]
    assert not any(key in prop["description"] for key in ("aria", "kael", "mira"))
    eligible = [member for member in members if not member["muted"]]
    instruction = speaking_plan_instruction(", ".join(member["speaker_key"] for member in eligible))
    assert "aria, kael" in instruction and "mira" not in instruction
    assert parse_speaking_plan([], members, 3) == []
    assert parse_speaking_plan(["unknown — wait"], members, 3) is None
    parsed = parse_speaking_plan(["aria — first", "Aria: again", "kael - next", "mira — muted"], members, 3)
    assert [(member["id"], exchange) for member, exchange in parsed] == [("a", "first"), ("k", "next")]
    assert round_robin_member(members, [{"speaker_member_id": "a"}])["id"] == "k"


def test_speaking_plan_resolves_hyphenated_speaker_keys_and_names():
    """A kebab-cased key is the shape `build_direct_scene_override` asks for.

    Splitting a plan line on the first `-` used to consume the key itself, so
    every member whose display name had two words — which is what produces a
    hyphen in `allocate_speaker_key` — was dropped, and a plan of nothing but
    those read as malformed and fell back to round-robin.
    """
    used: set[str] = set()
    members = [
        {
            "id": mid,
            "speaker_key": allocate_speaker_key(name, used),
            "display_name": name,
            "active": 1,
            "muted": 0,
        }
        for mid, name in (("h", "Alice Hart"), ("p", "Jean-Luc Picard"), ("a", "Arianna"))
    ]
    assert [m["speaker_key"] for m in members] == ["alice-hart", "jean-luc-picard", "arianna"]

    by_key = parse_speaking_plan(["alice-hart — steps forward", "jean-luc-picard — answers coldly"], members, 3)
    assert [(m["id"], exchange) for m, exchange in by_key] == [("h", "steps forward"), ("p", "answers coldly")]

    # Display names work too, and so do the other two separators the model reaches for.
    by_name = parse_speaking_plan(["Jean-Luc Picard: nods", "Alice Hart - waits"], members, 3)
    assert [(m["id"], exchange) for m, exchange in by_name] == [("p", "nods"), ("h", "waits")]

    # A longer key is never truncated into a shorter member's, in either direction.
    assert [m["id"] for m, _ in parse_speaking_plan(["arianna — waves"], members, 3)] == ["a"]
    # And a bare speaker with no exchange still resolves.
    assert [(m["id"], exchange) for m, exchange in parse_speaking_plan(["alice-hart"], members, 3)] == [("h", "")]


def test_plan_cue_reads_the_cue_for_a_speaker_cast_without_the_plan():
    """A pin decides *who*; the Director still decides *what* for that speaker.

    Regenerate, magic rewrite, `/speak`, a manual pick and round-robin all settle
    the speaker before the plan is read. They used to hand the writer an empty
    cue even when the Director had just written one for that exact member, so a
    regenerated reply was composed blind while the injected scene direction was
    aimed at whoever the plan opened with.
    """
    members = [
        {"id": "a", "speaker_key": "aria", "display_name": "Aria", "active": 1, "muted": 0},
        {"id": "k", "speaker_key": "kael", "display_name": "Kael", "active": 1, "muted": 0},
        {"id": "m", "speaker_key": "mira", "display_name": "Mira", "active": 1, "muted": 1},
    ]
    plan = ["aria — deflect the accusation", "kael — explode at her calm"]
    assert plan_cue(plan, members, "a") == "deflect the accusation"
    # Position in the plan is irrelevant: this member is speaking either way.
    assert plan_cue(plan, members, "k") == "explode at her calm"

    # Uncapped: `group_max_speakers` bounds who is cast, and nobody is being cast here.
    assert parse_speaking_plan(plan, members, 1) == [(members[0], "deflect the accusation")]
    assert plan_cue(plan, members, "k") == "explode at her calm"

    # Nothing to read: a plan that never named this member, a muted one, a rest,
    # a Director that declined the field, and a turn that never ran one.
    assert plan_cue(["aria — deflect the accusation"], members, "k") == ""
    assert plan_cue(["mira — muted"], members, "m") == ""
    assert plan_cue([], members, "a") == ""
    assert plan_cue(None, members, "a") == ""
    assert plan_cue("aria — not a list", members, "a") == ""


def test_every_director_seed_field_is_a_turn_state_field_and_is_copied_not_shared():
    """The seed is what speakers 2..n of one exchange start from.

    Two assertions, both about drift: the field list has to name real
    ``TurnState`` fields (it stands in for the orchestrator's old hand-kept
    copy), and each speaker has to get its *own* containers, since the
    once-per-exchange steps that run on the last speaker mutate them in place
    and would otherwise reach back into a reply already on the wire.
    """
    shared = TurnState(active_moods=["tense"], calls=[{"name": "direct_scene"}], macro_choices={"f": "a"})
    shared.direction_notes = [{"text": "note"}]
    for name in _DIRECTOR_SEED_FIELDS:
        assert hasattr(shared, name), name

    first, second = TurnState(), TurnState()
    first.seed_from(shared)
    second.seed_from(shared)
    first.calls.append({"name": "update_character_sheet"})
    first.direction_notes.append({"text": "later"})

    assert second.calls == [{"name": "direct_scene"}]
    assert second.direction_notes == [{"text": "note"}]
    assert shared.calls == [{"name": "direct_scene"}]
    assert second.active_moods == ["tense"] and second.macro_choices == {"f": "a"}
