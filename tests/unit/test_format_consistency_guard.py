from __future__ import annotations

import pytest

from backend.workflows.format_consistency.guard import rejection, unwrap

DRAFT = 'She crossed the room slowly. *He waits by the door.* "Good night," she said.'
FAITHFUL = 'I crossed the room slowly. *He waits by the door.* "Good night," I said.'


def test_a_faithful_restatement_is_accepted():
    assert rejection(DRAFT, FAITHFUL) == ""


def test_an_empty_rewrite_is_rejected():
    assert rejection(DRAFT, "   \n  ") == "empty"


# ---------- editor constraints ----------


def test_a_rewrite_that_grows_is_rejected():
    grown = (
        "I crossed the wide and silent room, slowly and without making a single sound. "
        '*He waits by the door, patient as ever.* "Good night to you," I said to him.'
    )
    assert rejection(DRAFT, grown).startswith("grew from")


def test_a_word_or_two_of_slack_is_allowed():
    assert rejection(DRAFT, FAITHFUL.replace("I crossed", "I am crossing")) == ""


def test_the_growth_slack_scales_with_the_draft():
    draft = " ".join([DRAFT] * 27)  # 378 words: 5% slack allows up to 396
    rewrite = " ".join([FAITHFUL] * 27)
    assert rejection(draft, f"{rewrite} and then some more") == ""
    assert rejection(draft, f"{rewrite} {' '.join(['again'] * 20)}").startswith("grew from")


def test_a_truncated_rewrite_is_rejected():
    assert rejection(DRAFT, "I crossed the room slowly.").startswith("shrank from")


def test_a_merged_paragraph_is_rejected():
    draft = "She waits by the tall window.\n\nHe arrives without a sound.\n\nThey leave together at last."
    merged = "I wait by the tall window. He arrives without a sound. We leave together at last."
    assert rejection(draft, merged) == "paragraph count 3 -> 1"


def test_a_reintroduced_speaker_label_is_rejected():
    assert rejection(DRAFT, f"Heidi: {FAITHFUL}") == "added a speaker label"


def test_a_label_the_draft_already_had_is_not_held_against_the_rewrite():
    assert rejection(f"Heidi: {DRAFT}", f"Heidi: {FAITHFUL}") == ""


# ---------- story content ----------


def test_reworded_dialogue_is_rejected():
    assert rejection(DRAFT, FAITHFUL.replace("Good night", "Goodbye")).startswith("dropped or reworded")


def test_dropped_dialogue_is_rejected():
    assert rejection(DRAFT, "I crossed the room slowly. *He waits by the door.* I said good night.").startswith(
        "dropped or reworded"
    )


def test_requoting_dialogue_is_not_a_content_change():
    bare = "*I crossed the room slowly.* He waits by the door. Good night, I said."
    assert rejection(DRAFT, bare) == ""


@pytest.mark.parametrize(
    ("protected", "mangled"),
    [
        ("```python\nx = 1\n```", "python\nx = 1"),
        ("***He could not believe it.***", "*He could not believe it.*"),
        ("***", "* * *"),
    ],
)
def test_mangling_a_protected_run_is_rejected(protected, mangled):
    draft = f"{DRAFT}\n\n{protected}"
    assert rejection(draft, f"{FAITHFUL}\n\n{mangled}") == "protected markup changed"


def test_a_protected_run_carried_through_is_accepted():
    draft = f"{DRAFT}\n\n```python\nx = 1\n```"
    assert rejection(draft, f"{FAITHFUL}\n\n```python\nx = 1\n```") == ""


# ---------- a tool call echoed into the argument ----------


@pytest.mark.parametrize(
    "leaked",
    [
        f'voice_rewrite("{FAITHFUL}")',
        f"voice_rewrite('{FAITHFUL}')",
        f'voice_rewrite("""{FAITHFUL}""")',
        f'voice_rewrite(rewritten_text="{FAITHFUL}")',
        f"`voice_rewrite({FAITHFUL})`",
        f'  voice_rewrite( "{FAITHFUL}" )\n',
    ],
)
def test_a_whole_passage_call_wrapper_is_peeled(leaked):
    assert unwrap(DRAFT, leaked) == FAITHFUL


def test_a_passage_that_opens_on_dialogue_keeps_its_quotes():
    draft = '"Good night," she said. *He waits by the door.*'
    restated = '"Good night," I said. *He waits by the door.*'
    assert unwrap(draft, f'voice_rewrite("{restated}")') == restated


def test_an_unwrapped_rewrite_is_left_alone():
    assert unwrap(DRAFT, FAITHFUL) == FAITHFUL


def test_a_draft_that_is_itself_the_call_is_left_alone():
    draft = 'voice_rewrite("hello")'
    assert unwrap(draft, draft) == draft


def test_a_partial_call_echo_is_rejected():
    leaked = f'voice_rewrite("{FAITHFUL}'
    assert unwrap(DRAFT, leaked) == leaked
    assert rejection(DRAFT, leaked) == "echoed the tool call"
