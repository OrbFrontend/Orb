"""Failure-oriented tests for shared prose segmentation and markup parsing."""

from __future__ import annotations

from backend.analysis.detectors.anti_echo import detect_anti_echo
from backend.analysis.format_consistency import (
    Dialogue,
    classify_axes,
    normalize_to_baseline,
)
from backend.analysis.text.text_segmentation import (
    SENT_SPLIT,
    ends_with_question,
    extract_block_spans,
    extract_blocks,
    extract_narration,
    find_emphasis_spans,
    find_quote_spans,
    split_narration_sentences,
    split_segment_sentences,
    split_sentences,
    strip_ooc,
)


def test_nested_directional_quotes_stay_one_outer_dialogue_span():
    text = "“She called it ‘odd’ yesterday.” Then she left."
    assert [(start, end, text[start:end]) for start, end in find_quote_spans(text)] == [
        (0, 32, "“She called it ‘odd’ yesterday.”")
    ]
    assert extract_narration(text) == "Then she left."
    assert extract_blocks(text) == [
        ("SPEECH", "“She called it ‘odd’ yesterday.”"),
        ("NARRATION", "Then she left."),
    ]


def test_curly_apostrophe_does_not_close_curly_single_dialogue():
    text = "‘I don’t know,’ she said."
    assert [text[start:end] for start, end in find_quote_spans(text)] == ["‘I don’t know,’"]
    assert split_segment_sentences(text) == ["‘I don’t know,’", "she said."]


def test_plural_possessive_apostrophe_does_not_close_double_dialogue():
    text = "“The students’ books are here,” she said."
    assert [text[start:end] for start, end in find_quote_spans(text)] == ["“The students’ books are here,”"]


def test_escaped_straight_quotes_do_not_fragment_dialogue():
    text = 'She said "a \\"quoted\\" word" and left.'
    assert [text[start:end] for start, end in find_quote_spans(text)] == ['"a \\"quoted\\" word"']
    assert extract_narration(text) == "She said and left."


def test_measurement_marks_do_not_create_bogus_straight_quote_dialogue():
    text = 'The frame is 12" by 8" wide.'
    assert find_quote_spans(text) == []
    assert extract_narration(text) == text


def test_unclosed_quote_is_literal_and_cannot_swallow_rest_of_paragraph():
    text = "He said “Stop. Then he left."
    assert find_quote_spans(text) == []
    assert extract_narration(text) == text
    assert extract_blocks(text) == [("NARRATION", text)]
    assert classify_axes(text).dialogue == Dialogue.UNKNOWN


def test_guillemet_and_cjk_quotes_are_dialogue():
    for text, quoted in (("«Arrête!» Then.", "«Arrête!»"), ("「止まれ！」彼は叫んだ。", "「止まれ！」")):
        assert [text[start:end] for start, end in find_quote_spans(text)] == [quoted]
        assert extract_blocks(text)[0] == ("SPEECH", quoted)


def test_sentence_split_preserves_balanced_closing_markup():
    assert split_sentences("He said “Stop.” Then left.") == ["He said “Stop.”", "Then left."]
    assert split_sentences("*He stopped.* Then left.") == ["*He stopped.*", "Then left."]


def test_sentence_split_does_not_break_titles_initials_or_acronyms():
    assert split_sentences("Dr. Rivera met J. R. Hart. They spoke.") == [
        "Dr. Rivera met J. R. Hart.",
        "They spoke.",
    ]
    assert split_sentences("U.S. Army officers waited. Then left.") == [
        "U.S. Army officers waited.",
        "Then left.",
    ]


def test_sentence_split_handles_ambiguous_time_abbreviation_by_next_case():
    assert split_sentences("At 5 p.m. before dusk, she left.") == ["At 5 p.m. before dusk, she left."]
    assert split_sentences("It was 5 p.m. She left.") == ["It was 5 p.m.", "She left."]


def test_sentence_split_supports_unicode_terminators_and_questions():
    assert split_sentences("彼は叫んだ。 Then prose.") == ["彼は叫んだ。", "Then prose."]
    assert split_sentences("真的？ نعم؟ Fine.") == ["真的？", "نعم؟", "Fine."]
    assert ends_with_question("真的？！”") is True


def test_cjk_sentences_split_without_intervening_ascii_whitespace():
    assert split_sentences("彼は帰った。彼女は残った。") == ["彼は帰った。", "彼女は残った。"]


def test_line_breaks_are_hard_sentence_boundaries_without_punctuation():
    text = "First fragment\nSecond fragment\r\nThird fragment\u2028Fourth fragment"
    expected = ["First fragment", "Second fragment", "Third fragment", "Fourth fragment"]
    assert split_sentences(text) == expected
    assert split_narration_sentences(text) == expected
    assert split_segment_sentences(text) == expected
    assert [part for part in SENT_SPLIT.split(text) if part] == expected
    assert all(not any(mark in sentence for mark in "\r\n\u2028") for sentence in expected)


def test_line_break_between_dialogue_lines_cannot_form_one_sentence():
    text = '"First line."\n"Second line."'
    assert split_sentences(text) == ['"First line."', '"Second line."']
    assert split_segment_sentences(text) == ['"First line."', '"Second line."']


def test_narration_fragments_around_dialogue_are_source_substrings_not_fused():
    text = 'She said "hello" to him. Then she left.'
    fragments = split_narration_sentences(text)
    assert fragments == ["She said", "to him.", "Then she left."]
    assert all(fragment in text for fragment in fragments)


def test_quote_terminal_does_not_fuse_narration_across_dialogue():
    text = 'He warned her, "Run!" Then the door opened.'
    assert split_narration_sentences(text) == ["He warned her,", "Then the door opened."]


def test_single_marker_thought_is_parsed_but_markdown_bold_and_escapes_are_not():
    text = r"*thought* **bold** __also bold__ \*literal\*"
    assert [text[start:end] for start, end in find_emphasis_spans(text)] == ["*thought*"]


def test_emphasis_straddling_dialogue_cannot_hide_the_speech_span():
    text = '*before "speech" after*'
    spans = extract_block_spans(text)
    assert "".join(text[start:end] for _typ, start, end in spans) == text
    assert any(typ == "SPEECH" and text[start:end] == '"speech"' for typ, start, end in spans)


def test_markdown_bold_survives_format_normalization_byte_for_byte():
    baseline = ['She nods. "Hello."']
    draft = '*He leans in.* **This stays bold.** "Hi."'
    normalized, report = normalize_to_baseline(draft, baseline, enabled=True)
    assert report.changed is True
    assert normalized == 'He leans in. **This stays bold.** "Hi."'


def test_strip_ooc_preserves_ordinary_brackets_and_balances_nested_ooc():
    text = 'Keep [door slams] this [OOC: remove [nested] "instruction"] end.'
    assert strip_ooc(text) == "Keep [door slams] this   end."


def test_strip_ooc_removes_unclosed_tagged_tail_but_not_unclosed_stage_direction():
    assert strip_ooc("Keep [OOC: remove the rest") == "Keep  "
    assert strip_ooc("Keep [door opens") == "Keep [door opens"


def test_nested_curly_dialogue_still_triggers_anti_echo():
    result = detect_anti_echo("‘No money?’ she repeats.", "‘I don’t have any money.’")
    assert len(result.flagged_echoes) == 1
    assert result.flagged_echoes[0].matched_phrase == "money"
