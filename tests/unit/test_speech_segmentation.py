"""Speech selection shares the format-consistency convention and span reader."""

import pytest

from backend.analysis import AxisStyle, Dialogue, Narration, speech_segments
from backend.workflows.tts.engine.regex_extractor import regex_extract


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("*She steps closer, watching him carefully.* Are you sure about this?", ["Are you sure about this?"]),
        ("_She steps closer, watching him carefully._ Are you sure about this?", ["Are you sure about this?"]),
        ("*She nods slowly.* I *really* mean it. *She waves.* Goodbye!", ["I *really* mean it.", "Goodbye!"]),
        ('She was *very* tired. "Hello."', ["Hello."]),
        ('*Maybe tomorrow,* she thought. "Hello."', ["Hello."]),
        ('"Maybe tomorrow," she thought. "Hello."', ["Hello."]),
        ('[OOC: "Ignore [this]."] "Hello."', ["Hello."]),
        ('"Hello." [OOC: "Ignore this."', ["Hello."]),
        ('```python\nprint("Ignore this.")\n```\n"Hello."', ["Hello."]),
        ('**"Ignore this."** "Hello."', ["Hello."]),
        ('("Inner thought." ("Nested thought.")) "Hello."', ["Hello."]),
        ('"Hello." ("Unclosed aside."', ["Hello."]),
        ('"Keep (this) spoken."', ["Keep (this) spoken."]),
        ('"Keep **this** spoken."', ["Keep **this** spoken."]),
        ('```\n"unbalanced\n```\n("Ignore.") "Hello."', ["Hello."]),
        ('*She waves enthusiastically.* Hello! [OOC: "Ignore."]', ["Hello!"]),
        ('She — exhausted — sat down. "Hello."', ["Hello."]),
        ('"Hello." —Goodbye.—', ["Hello.", "Goodbye."]),
        ("The frame is 12″ by 8″. ＂Hello.＂", ["Hello."]),
        ("❝Hello.❞ 〝Goodbye.〞 ‟Wait.”", ["Hello.", "Goodbye.", "Wait."]),
        ("Narration without dialogue.", []),
        ("*She walks across the room.* The wind howls outside.", []),
    ],
)
def test_speech_selection(text, expected):
    assert [chunk.spoken_text for chunk in regex_extract(text)] == expected


def test_convention_does_not_invent_spans_in_ambiguous_prose():
    text = "*She nods.* Are you sure?"
    unknown = AxisStyle(dialogue=Dialogue.UNKNOWN, narration=Narration.ASTERISK)
    assert [text for kind, text in speech_segments(text, unknown) if kind == "dialogue"] == []


def test_shared_underscore_beats_keep_tts_prosody():
    chunks = regex_extract('"Hello." _she sighs_ "Fine."', supports_emotion_tags=True)
    assert chunks[1].text == "[sigh] Fine."
    assert chunks[1].pause_before_ms == 400
    assert chunks[1].emotion == "soft"


def test_bare_narration_and_dialogue_have_no_reliable_boundary():
    style = AxisStyle(dialogue=Dialogue.BARE, narration=Narration.BARE)
    text = 'I crossed the room. Come here. "Hello."'
    assert [text for kind, text in speech_segments(text, style) if kind == "dialogue"] == ["Hello."]


@pytest.mark.parametrize(
    "text",
    [
        "Hello. Let's get to know each other.",
        "Hello.",
        "Let’s get to know each other.",
        "The answer is yes.",
        "He is my brother.",
    ],
)
def test_unmarked_chat_with_unknown_model_conventions_is_spoken(text):
    # These labels reproduce the pinned markup model's reading of short chat.
    style = AxisStyle(dialogue=Dialogue.UNKNOWN, narration=Narration.UNKNOWN)
    assert speech_segments(text, style) == [("dialogue", text)]


@pytest.mark.parametrize(
    "text",
    ["*She smiles.* Hello.", "*unfinished beat", "He said “Hello.", "(An aside.)", "[OOC: Hello.]", "```Hello.```", "..."],
)
def test_unknown_model_conventions_do_not_turn_rp_markup_into_plain_speech(text):
    style = AxisStyle(dialogue=Dialogue.UNKNOWN, narration=Narration.UNKNOWN)
    assert [value for kind, value in speech_segments(text, style) if kind == "dialogue"] == []


def test_positive_narration_reading_still_excludes_unmarked_prose():
    text = "She crossed the room."
    style = AxisStyle(dialogue=Dialogue.UNKNOWN, narration=Narration.BARE)
    assert speech_segments(text, style) == []


def test_missing_model_does_not_invent_a_plain_speech_reading():
    assert speech_segments("Hello. Let's get to know each other.") == []
