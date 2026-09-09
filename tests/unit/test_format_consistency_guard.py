"""Unit tests for the voice rewrite's acceptance guard.

The forced call runs after the Editor and after the pipeline stripped the
model's self-label, so its answer is the reply with nothing behind it. These pin
what the workflow will and will not let stand in for a finished draft.

The rule throughout is comparative: nothing here is an absolute limit on a
reply, only a limit on how far a *restatement* may move from the draft it was
handed.
"""

from __future__ import annotations

import pytest

from backend.workflows.format_consistency.guard import rejection

DRAFT = 'She crossed the room slowly. *He waits by the door.* "Good night," she said.'
FAITHFUL = 'I crossed the room slowly. *He waits by the door.* "Good night," I said.'


def test_a_faithful_restatement_is_accepted():
    assert rejection(DRAFT, FAITHFUL) == ""


def test_an_empty_rewrite_is_rejected():
    assert rejection(DRAFT, "   \n  ") == "empty"


# ---------- what the Editor already decided ----------


def test_a_rewrite_that_grows_is_rejected():
    # The length guard is a ceiling: the Editor may have just rewritten this draft
    # to get under it, and a voice fix that pushes back over undoes that pass.
    grown = (
        "I crossed the wide and silent room, slowly and without making a single sound. "
        '*He waits by the door, patient as ever.* "Good night to you," I said to him.'
    )
    assert rejection(DRAFT, grown).startswith("grew from")


def test_a_word_or_two_of_slack_is_allowed():
    # A tense change can need an auxiliary; that is not the model doing something else.
    assert rejection(DRAFT, FAITHFUL.replace("I crossed", "I am crossing")) == ""


def test_a_truncated_rewrite_is_rejected():
    assert rejection(DRAFT, "I crossed the room slowly.").startswith("shrank from")


def test_a_merged_paragraph_is_rejected():
    # Same words, one fewer break: the reply's shape is part of what the Editor
    # and the length guard settled, and a restatement does not get to re-block it.
    draft = "She waits by the tall window.\n\nHe arrives without a sound.\n\nThey leave together at last."
    merged = "I wait by the tall window. He arrives without a sound. We leave together at last."
    assert rejection(draft, merged) == "paragraph count 3 -> 1"


def test_a_reintroduced_speaker_label_is_rejected():
    # The pipeline strips this after the Editor and before workflows; nothing runs
    # after the rewrite to strip it a second time.
    assert rejection(DRAFT, f"Monika: {FAITHFUL}") == "added a speaker label"


def test_a_label_the_draft_already_had_is_not_held_against_the_rewrite():
    assert rejection(f"Monika: {DRAFT}", f"Monika: {FAITHFUL}") == ""


# ---------- story content ----------


def test_reworded_dialogue_is_rejected():
    assert rejection(DRAFT, FAITHFUL.replace("Good night", "Goodbye")).startswith("dropped or reworded")


def test_dropped_dialogue_is_rejected():
    assert rejection(DRAFT, "I crossed the room slowly. *He waits by the door.* I said good night.").startswith(
        "dropped or reworded"
    )


def test_requoting_dialogue_is_not_a_content_change():
    # The markup pass runs after this one and is the authority on quotes vs.
    # asterisks. The guard asks whether the line is still said, not how it is
    # marked -- otherwise it would reject the very drift the next pass repairs.
    bare = "*I crossed the room slowly.* He waits by the door. Good night, I said."
    assert rejection(DRAFT, bare) == ""


@pytest.mark.parametrize(
    ("protected", "mangled"),
    [
        ("```python\nx = 1\n```", "python\nx = 1"),  # fence markers dropped
        ("***He could not believe it.***", "*He could not believe it.*"),  # bold flattened
        ("***", "* * *"),  # divider respelled
    ],
)
def test_mangling_a_protected_run_is_rejected(protected, mangled):
    # These runs are literal: the markup pass carries them through byte for byte,
    # and a model asked to restate the prose has no business in them either.
    draft = f"{DRAFT}\n\n{protected}"
    assert rejection(draft, f"{FAITHFUL}\n\n{mangled}") == "protected markup changed"


def test_a_protected_run_carried_through_is_accepted():
    draft = f"{DRAFT}\n\n```python\nx = 1\n```"
    assert rejection(draft, f"{FAITHFUL}\n\n```python\nx = 1\n```") == ""
