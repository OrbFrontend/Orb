"""The public-profile output contract, pinned on both draft functions.

The system prompt asks the model for a scene-safe two-liner; this is the half
that does not depend on the model agreeing. Both the card editor and Manage cast
drain the same forced call through the same checks, so exercising both public
entry points is what stops the card and scene routes drifting on a safety
boundary that only one of them is loudly tested for.

Three rejections, each for a failure a later reader cannot recover from:

* a blank field silently publishes nothing about that member;
* a brace survives into a string that is macro-resolved at *turn* time
  (``inference/group_context._render_public_cast``), so an approved profile
  would mutate months later;
* an overlong field is billed to every member of the cast on every call.
"""

from __future__ import annotations

import json

import pytest

from backend.features.cards.public_profile import (
    MAX_FIELD_WORDS,
    PROFILE_FLOOR,
    PROFILE_TOOL_NAME,
    ProfileDraftUnavailable,
    build_scene_message,
    draft_card_profile,
    draft_scene_profile,
)

CARD = {"name": "Aria", "description": "A scout of the northern watch.", "personality": "Wary"}


class _FakeClient:
    """Yields one forced-call ``done`` message, the way ``LLMClient`` does."""

    def __init__(self, message: dict) -> None:
        self.message = message
        self.calls: list[dict] = []

    async def complete(self, *, messages, model, tools, tool_choice, **params):
        self.calls.append({"messages": messages, "model": model, "tools": tools, "tool_choice": tool_choice, **params})
        yield {"type": "done", "message": self.message}


def _call(name: str = PROFILE_TOOL_NAME, **arguments) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


async def _draft_both(message: dict):
    """Run the same response through both public entry points."""
    return [
        await draft_card_profile(_FakeClient(message), "m", CARD),  # type: ignore[arg-type]
        await draft_scene_profile(_FakeClient(message), "m", CARD),  # type: ignore[arg-type]
    ]


# ── Accepted ────────────────────────────────────────────────────────────────


async def test_a_well_formed_draft_round_trips_stripped():
    for draft in await _draft_both(_call(appearance="Tall, in road-worn green.", role="Wandering bard.")):
        assert draft == {"appearance": "Tall, in road-worn green.", "role": "Wandering bard."}


async def test_whitespace_and_line_breaks_collapse_to_one_line():
    """The rendered profile is one labelled line per field; a field carrying its
    own newlines would break that shape wherever it is assembled."""
    for draft in await _draft_both(_call(appearance="  Tall,\n  in green.\t", role="A bard.\n\nOf the road.")):
        assert draft == {"appearance": "Tall, in green.", "role": "A bard. Of the road."}


async def test_json_string_arguments_parse_the_same_way():
    """Providers send `arguments` as a JSON string as often as a dict."""
    message = {
        "role": "assistant",
        "tool_calls": [
            {"function": {"name": PROFILE_TOOL_NAME, "arguments": json.dumps({"appearance": "Tall.", "role": "Bard."})}}
        ],
    }
    for draft in await _draft_both(message):
        assert draft == {"appearance": "Tall.", "role": "Bard."}


# ── Rejected ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("arguments", "because"),
    [
        ({"appearance": "", "role": "Bard."}, "empty appearance"),
        ({"appearance": "Tall.", "role": "   "}, "whitespace-only role"),
        ({"appearance": "Tall."}, "missing role"),
        ({"role": "Bard."}, "missing appearance"),
        ({"appearance": None, "role": "Bard."}, "null appearance"),
        ({"appearance": ["Tall."], "role": "Bard."}, "non-string appearance"),
        ({"appearance": "Tall as {{user}}.", "role": "Bard."}, "a macro"),
        ({"appearance": "Tall.", "role": "Bard to {{char}}."}, "a macro in the second field"),
        ({"appearance": "Tall {", "role": "Bard."}, "a bare brace"),
        ({"appearance": " ".join(f"w{i}" for i in range(MAX_FIELD_WORDS + 1)), "role": "Bard."}, "an overlong field"),
    ],
)
async def test_a_malformed_draft_is_unavailable_rather_than_stored(arguments, because):
    for drafter in (draft_card_profile, draft_scene_profile):
        with pytest.raises(ProfileDraftUnavailable):
            await drafter(_FakeClient(_call(**arguments)), "m", CARD)  # type: ignore[arg-type]
    assert because  # names the case in the failure output


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "assistant", "content": "Appearance: tall. Role: bard."},
        _call(name="some_other_tool", appearance="Tall.", role="Bard."),
    ],
)
async def test_an_answer_with_no_usable_call_is_unavailable(message):
    for drafter in (draft_card_profile, draft_scene_profile):
        with pytest.raises(ProfileDraftUnavailable, match="did not return a usable profile"):
            await drafter(_FakeClient(message), "m", CARD)  # type: ignore[arg-type]


# ── The prompts ─────────────────────────────────────────────────────────────


async def test_both_prompts_quote_the_same_no_secrets_floor():
    """One definition of "public", so the card editor and Manage cast cannot
    drift on what a profile is allowed to say."""
    systems = []
    for drafter in (draft_card_profile, draft_scene_profile):
        client = _FakeClient(_call(appearance="Tall.", role="Bard."))
        await drafter(client, "m", CARD)  # type: ignore[arg-type]
        systems.append(client.calls[0]["messages"][0]["content"])
    assert all(PROFILE_FLOOR in system for system in systems)
    # The scene prompt adds the sentence that buys mode-independence.
    assert "no matter which member is currently speaking" in systems[1]
    assert "no matter which member is currently speaking" not in systems[0]


async def test_the_drafting_call_is_forced_and_not_at_the_writing_preset():
    """A roleplay preset at temperature 1.15 would produce a florid two-liner;
    this is a summarization call, so the hyperparameters are hardcoded."""
    client = _FakeClient(_call(appearance="Tall.", role="Bard."))
    await draft_scene_profile(client, "m", CARD)  # type: ignore[arg-type]
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "function", "function": {"name": PROFILE_TOOL_NAME}}
    assert call["temperature"] == 0.2 and call["max_tokens"] == 512


def test_the_scene_message_labels_its_sections_and_omits_the_empty_ones():
    message = build_scene_message(
        {"name": "Aria", "description": "A scout.", "post_history_instructions": "Write her terse."},
        display_name="Aria of the Watch",
        cast_names=["Kael", "Mira"],
        premise="A cold night on the wall.",
        card_profile="Appearance: Tall.\nRole: Scout.",
    )
    assert "Scene premise" in message and "A cold night on the wall." in message
    assert "Kael, Mira" in message
    assert "Aria of the Watch" in message
    # A directive is about *how to write*, not a fact about the character.
    assert "private directives" in message and "Write her terse." in message
    assert "the default this scene's profile replaces" in message
    assert "Adjust that default" in message
    # Nothing was said about personality or examples, so neither is labelled.
    assert "personality" not in message and "example dialogue" not in message


def test_the_scene_message_states_how_many_names_it_left_out():
    """The prompt is bounded, and says so rather than claiming the list is the
    whole cast — the roster itself has no size ceiling."""
    message = build_scene_message(CARD, cast_names=["Kael"], omitted_cast=3)
    assert "Other cast members omitted from this draft: 3" in message
    assert "omitted from this draft" not in build_scene_message(CARD, cast_names=["Kael"])

