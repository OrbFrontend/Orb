"""The decision renderer: one frozen snapshot, one pass, an explicit macro set.

The two properties worth guarding here are the ones the plan spells out and that
an ordinary macro resolver would violate: authored tokens resolve exactly once,
and a macro the scope cannot supply raises rather than rendering empty.
"""

from __future__ import annotations

import pytest

from backend.core import DEFAULT_STATE_TEMPLATE, Macros
from backend.pipeline.passes.decisions import (
    STATE_MACROS,
    TEXT_MACROS,
    DecisionSnapshot,
    build_snapshot,
    macro_errors,
    macros_used,
    render,
    template_errors,
)
from backend.pipeline.passes.decisions.render import (
    RECENT_HISTORY_DEPTH,
    UnavailableMacro,
    oversized_question,
    oversized_state,
)

MACROS = Macros(user="Tester", char="Maren", description="A wizard.")


def _snapshot(**overrides) -> DecisionSnapshot:
    base = {
        "last_message": "I shove the door.",
        "last_assistant_message": "She braces against the frame.",
        "user": "Tester",
        "char": "Maren",
        "description": "A wizard.",
    }
    base.update(overrides)
    return DecisionSnapshot(**base)


# ── the default template, exactly ────────────────────────────────────────────


def test_the_default_template_renders_the_documented_text():
    assert render(DEFAULT_STATE_TEMPLATE, _snapshot()) == (
        "Previous reply:\nShe braces against the frame.\n\nCurrent request:\nI shove the door."
    )


def test_missing_prior_history_renders_as_an_empty_previous_reply():
    assert render(DEFAULT_STATE_TEMPLATE, _snapshot(last_assistant_message="")) == (
        "Previous reply:\n\n\nCurrent request:\nI shove the door."
    )


# ── one pass, never recursive ────────────────────────────────────────────────


def test_message_bodies_are_inserted_as_opaque_values():
    # A character wrote "{{char}}" in a reply. It is prose, not a macro: expanding
    # it would let the story rewrite the classifier's question.
    snapshot = _snapshot(last_assistant_message="She said the word {{char}} aloud, and then {{random::a::b}}.")
    rendered = render(DEFAULT_STATE_TEMPLATE, snapshot)
    assert "{{char}}" in rendered
    assert "{{random::a::b}}" in rendered
    assert "Maren aloud" not in rendered


def test_backticked_macros_stay_literal():
    assert render("Write `{{last_message}}` to mean the request.", _snapshot()) == (
        "Write `{{last_message}}` to mean the request."
    )


def test_unsupported_macros_are_left_raw_rather_than_blanked():
    # Validation is what rejects them; the renderer must not silently delete an
    # author's text on a path that skipped it.
    assert render("A {{roll::1d6}} and a {{nonsense}}", _snapshot()) == "A {{roll::1d6}} and a {{nonsense}}"


def test_text_fields_resolve_only_the_three_identity_macros():
    rendered = render("Does {{char}} beat {{user}}? {{last_message}}", _snapshot(), allowed=TEXT_MACROS)
    assert rendered == "Does Maren beat Tester? {{last_message}}"


# ── an unavailable macro raises rather than rendering empty ──────────────────


def test_description_is_unavailable_at_the_group_exchange_stage():
    group = _snapshot(description=None, scope="group")
    with pytest.raises(UnavailableMacro) as raised:
        render("About {{description}}", group)
    assert raised.value.macro == "description"


def test_an_empty_description_is_not_the_same_as_an_unavailable_one():
    assert render("About {{description}}", _snapshot(description="")) == "About "


# ── validation ───────────────────────────────────────────────────────────────


def test_macros_used_is_ordered_deduplicated_and_ignores_literals():
    assert macros_used("{{char}} and {{user}} and {{char}} but not `{{cast}}`") == ["char", "user"]


def test_later_stage_macros_get_their_own_explanation():
    problems = template_errors("{{draft}}")
    assert problems and "before the Director" in problems[0]


def test_an_empty_template_is_an_error_but_an_empty_output_is_not():
    assert template_errors("  ") == ["Template must not be empty"]
    assert macro_errors("") == []


def test_every_supported_state_macro_resolves():
    snapshot = DecisionSnapshot(
        last_message="a", last_assistant_message="b", recent_history="c", user="d", char="e", cast="f", description="g"
    )
    for macro in STATE_MACROS:
        assert render(f"<{{{{{macro}}}}}>", snapshot) != f"<{{{{{macro}}}}}>", macro


# ── size limits ──────────────────────────────────────────────────────────────


def test_size_limits_are_measured_in_utf8_bytes():
    assert not oversized_state("é" * 8_000)
    assert oversized_state("é" * 8_193)
    assert not oversized_question("x" * 100, {"true": "y" * 100, "false": "z" * 100})
    assert oversized_question("x" * 8_192, {"true": "y", "false": "z"})


# ── snapshot construction ────────────────────────────────────────────────────


def _history(*rows) -> list[dict]:
    return [dict(row) for row in rows]


def test_solo_snapshot_reads_the_last_assistant_message_unlabelled():
    snapshot = build_snapshot(
        history=_history(
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ),
        current_request="three",
        macros=MACROS,
        scope="solo",
        description="A wizard.",
    )
    assert snapshot.last_assistant_message == "two"
    assert snapshot.last_message == "three"
    assert snapshot.description == "A wizard."


def test_group_snapshot_labels_speakers_and_withholds_the_description():
    snapshot = build_snapshot(
        history=_history(
            {"role": "user", "content": "who is there"},
            {"role": "assistant", "content": "me", "speaker_member_id": "m1"},
        ),
        current_request="hello",
        macros=MACROS,
        scope="group",
        speaker_names={"m1": "Maren"},
        description=None,
    )
    assert snapshot.last_assistant_message == "Maren: me"
    assert snapshot.recent_history.endswith("Maren: me")
    # No selected speaker at the exchange stage, so no single card to read.
    assert snapshot.description is None


def test_recent_history_is_the_last_four_completed_messages_oldest_first():
    rows = _history(*({"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(8)))
    snapshot = build_snapshot(history=rows, current_request="now", macros=MACROS, scope="solo")
    lines = snapshot.recent_history.split("\n\n")
    assert len(lines) == RECENT_HISTORY_DEPTH
    assert [line.split(": ", 1)[1] for line in lines] == ["4", "5", "6", "7"]
    # The current request is not part of history; it has its own macro.
    assert "now" not in snapshot.recent_history


def test_attachment_bytes_never_reach_the_classifier_state():
    snapshot = build_snapshot(
        history=_history(
            {
                "role": "user",
                "content": "look at this",
                "user_attachments": [{"mime_type": "image/png", "data_b64": "AAAABBBBCCCC"}],
            },
            {"role": "assistant", "content": "I see it."},
        ),
        current_request="and now?",
        macros=MACROS,
        scope="solo",
    )
    assert "AAAABBBBCCCC" not in snapshot.recent_history
    assert "look at this" in snapshot.recent_history


def test_steering_reaches_the_state_through_the_current_request():
    # Magic Rewrite and super-regenerate send an OOC message as the writer input.
    # It must not disappear from the classifier's view.
    snapshot = build_snapshot(
        history=_history({"role": "assistant", "content": "the reply being replaced"}),
        current_request="[OOC: Rewrite it darker.]",
        macros=MACROS,
        scope="solo",
    )
    assert "[OOC: Rewrite it darker.]" in render(DEFAULT_STATE_TEMPLATE, snapshot)
