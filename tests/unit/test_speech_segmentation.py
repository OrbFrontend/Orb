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
    style = AxisStyle(dialogue=Dialogue.UNKNOWN, narration=Narration.BARE)
    for text in ("She crossed the room.", "*She walks across the room.* The wind howls outside."):
        assert [value for kind, value in speech_segments(text, style) if kind == "dialogue"] == []


def test_missing_model_does_not_invent_a_plain_speech_reading():
    assert speech_segments("Hello. Let's get to know each other.") == []


@pytest.mark.parametrize(
    "line",
    [
        "The answer is yes.",
        "He is my brother.",
        "She is my sister.",
        "They are my friends.",
        "His name is Sam.",
        "Her name is Sam.",
        "Their names escape me.",
        "The answer is *definitely* yes.",
        "He is _really_ my brother.",
    ],
)
@pytest.mark.parametrize("use_model", [False, True])
def test_bare_speech_is_not_filtered_by_its_first_word(line, use_model):
    text = f"*She smiles warmly and settles back into her chair.* {line}"
    style = AxisStyle(Dialogue.BARE, Narration.ASTERISK) if use_model else None
    assert [chunk.spoken_text for chunk in regex_extract(text, style=style)] == [line]


@pytest.mark.parametrize("dialogue", list(Dialogue))
@pytest.mark.parametrize("narration", list(Narration))
@pytest.mark.parametrize(
    "text, expected",
    [
        ("—Hello.— —Goodbye.—", ["Hello.", "Goodbye."]),
        ("—Hello——Goodbye—", ["Hello", "Goodbye"]),
        ("*She sighs.* —Hello.— —Goodbye.—", ["Hello.", "Goodbye."]),
        ('"Hello." —Goodbye.—', ["Hello.", "Goodbye."]),
        ("—I *really* mean it.— —Goodbye.—", ["I *really* mean it.", "Goodbye."]),
        ("—I _really_ mean it.—", ["I _really_ mean it."]),
        ("—Hello.—\u2028—Goodbye.—", ["Hello.", "Goodbye."]),
        ('"Keep — this — spoken."', ["Keep — this — spoken."]),
        ("—Maybe tomorrow,— she thought.", []),
    ],
)
def test_dash_speech_is_independent_of_convention(text, expected, dialogue, narration):
    style = AxisStyle(dialogue, narration)
    assert [chunk.spoken_text for chunk in regex_extract(text, style=style)] == expected


@pytest.mark.parametrize(
    "text",
    [
        'She — exhausted — sat down. "Hello."',
        '*She — exhausted — sat down.* "Hello."',
        '(—An aside.—) "Hello."',
        '[OOC: —An aside.—] "Hello."',
        '```—Some code.—``` "Hello."',
        '**—A title.—** "Hello."',
        '—Unclosed dash. "Hello."',
        '—Split\nline.— "Hello."',
        '—Split\u2028line.— "Hello."',
        '*She walks across the room.* The wind howls outside. "Hello."',
    ],
)
def test_dash_scanning_preserves_narration_and_exclusions(text):
    style = AxisStyle(Dialogue.QUOTED, Narration.BARE)
    assert [chunk.spoken_text for chunk in regex_extract(text, style=style)] == ["Hello."]


def test_consecutive_dash_dialogue_keeps_beat_prosody():
    chunks = regex_extract("—Hello.— _she sighs_ —Fine.— —Goodbye.—", supports_emotion_tags=True)
    assert [chunk.text for chunk in chunks] == ["Hello.", "[sigh] Fine.", "Goodbye."]
    assert [chunk.pause_before_ms for chunk in chunks] == [0, 400, 300]


@pytest.mark.parametrize("thought", ['"Maybe tomorrow,"', "—Maybe tomorrow,—", "*Maybe tomorrow,*"])
@pytest.mark.parametrize("attribution", ["she thought.", "she thought with a *heavy* sigh."])
def test_bare_speech_does_not_read_thought_attributions(thought, attribution):
    text = f"*She smiles.* {thought} {attribution} The answer is yes."
    style = AxisStyle(Dialogue.BARE, Narration.ASTERISK)
    assert [chunk.spoken_text for chunk in regex_extract(text, style=style)] == ["The answer is yes."]


@pytest.mark.parametrize("line", ['"Hello."', "—Hello.—"])
def test_unpunctuated_thought_attribution_does_not_swallow_explicit_speech(line):
    text = f'"Maybe tomorrow," she thought with a sigh\n{line}'
    style = AxisStyle(Dialogue.BARE, Narration.ASTERISK)
    assert [chunk.spoken_text for chunk in regex_extract(text, style=style)] == ["Hello."]
