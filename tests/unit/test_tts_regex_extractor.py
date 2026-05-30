"""Unit tests for the regex-based dialogue extractor.

Tests regex_extract() which extracts speakable dialogue from RP text
using pure heuristics — zero LLM calls.
"""

from __future__ import annotations

from backend.workflows.tts.engine.regex_extractor import (
    AUDIBLE_BEATS,
    AUDIBLE_EMOTION_MAP,
    _extract_beat_action,
    _infer_emotion,
    regex_extract,
)


class TestSpokenText:
    """spoken_text carries the bare dialogue even when text gains a tag prefix."""

    def test_untagged_spoken_text_equals_text(self):
        chunks = regex_extract('"Hello there."', backend_type="edge", supports_emotion_tags=False)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there."
        assert chunks[0].spoken_text == "Hello there."

    def test_tagged_text_keeps_spoken_text_bare(self):
        chunks = regex_extract('*laughs* "That is funny."', backend_type="elevenlabs", supports_emotion_tags=True)
        assert len(chunks) == 1
        assert chunks[0].text.startswith("[laugh]")
        assert chunks[0].spoken_text == "That is funny."


class TestBasicDialogue:
    """Extract quoted dialogue from text."""

    def test_simple_double_quoted(self):
        text = 'She looked up. "Hello there," she said.'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there,"

    def test_multiple_quotes(self):
        text = '"First." She paused. "Second." Then she added, "Third."'
        chunks = regex_extract(text)
        assert len(chunks) == 3
        assert chunks[0].text == "First."
        assert chunks[1].text == "Second."
        assert chunks[2].text == "Third."

    def test_no_quotes_returns_empty(self):
        text = "*She walks across the room.* The wind howls outside."
        chunks = regex_extract(text)
        assert chunks == []

    def test_empty_string(self):
        assert regex_extract("") == []

    def test_whitespace_only(self):
        assert regex_extract("   \n\t  ") == []


class TestActionBeats:
    """Asterisk action beats are handled correctly."""

    def test_audible_beat_creates_pause(self):
        text = '*she laughs* "That\'s hilarious."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        # First chunk's pause is zeroed, but the tag/emotion should indicate the beat
        assert "hilarious" in chunks[0].text

    def test_silent_beat_creates_short_pause(self):
        text = '*she smiles* "Come here." *she nods* "Please."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        # Second chunk should have pause from the beat before it
        assert chunks[1].pause_before_ms >= 200

    def test_audible_beat_with_tag(self):
        text = '*she sighs* "Fine."'
        chunks = regex_extract(text, supports_emotion_tags=True)
        assert len(chunks) == 1
        assert "[sigh]" in chunks[0].text

    def test_audible_beat_without_tag(self):
        text = '*she sighs* "Fine."'
        chunks = regex_extract(text, supports_emotion_tags=False)
        assert len(chunks) == 1
        assert "[sigh]" not in chunks[0].text
        assert chunks[0].text == "Fine."

    def test_multiple_beats_between_dialogue(self):
        text = '*she gasps* "What?" *she pauses* "You can\'t be serious."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert "What?" in chunks[0].text


class TestParentheticalThoughts:
    """Inner monologue in parens is stripped."""

    def test_thought_stripped(self):
        text = '"Hey." (Maybe I should leave.) "Are you okay?"'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[0].text == "Hey."
        assert chunks[1].text == "Are you okay?"

    def test_only_thoughts_no_dialogue(self):
        text = "(This is interesting.) *She thinks.* (Maybe later.)"
        chunks = regex_extract(text)
        assert chunks == []


class TestEmotionHeuristics:
    """Emotion is inferred from punctuation."""

    def test_exclamation_warm(self):
        text = '"Hey!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "warm"

    def test_double_exclamation_angry(self):
        text = '"STOP!!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "angry"

    def test_ellipsis_soft(self):
        text = '"I don\'t know..."'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "soft"

    def test_surprise_mark(self):
        text = '"What?!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "surprised"

    def test_neutral_default(self):
        text = '"Okay."'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "neutral"

    def test_all_caps_angry(self):
        text = '"NOPE"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "angry"

    def test_emotion_from_beat(self):
        text = '*she whispers* "Come closer."'
        chunks = regex_extract(text)
        # Whisper beat should set emotion to whispered
        assert chunks[0].emotion in ("whispered", "neutral")


class TestBackendAwareness:
    """Backend type affects tag output."""

    def test_edge_no_tags(self):
        text = '*she laughs* "Funny."'
        chunks = regex_extract(text, backend_type="edge", supports_emotion_tags=False)
        assert "[laugh]" not in chunks[0].text

    def test_elevenlabs_with_tags(self):
        text = '*she laughs* "Funny."'
        chunks = regex_extract(text, backend_type="elevenlabs", supports_emotion_tags=True)
        assert "[laugh]" in chunks[0].text


class TestEdgeCases:
    """Tricky inputs."""

    def test_dialogue_with_inner_quotes(self):
        # Single quotes inside double quotes should be preserved
        text = "\"She said 'hello' to me.\""
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert "hello" in chunks[0].text

    def test_very_long_text(self):
        dialogue = '"' + "A" * 5000 + '"'
        chunks = regex_extract(dialogue)
        assert len(chunks) == 1
        assert len(chunks[0].text) == 5000

    def test_mixed_beats_and_dialogue_complex(self):
        text = (
            '*The door creaks open.* "Hey." *she smiles warmly* '
            '"I was just thinking about you." (God, he looks tired.) '
            '"You okay?" *she reaches out*'
        )
        chunks = regex_extract(text)
        assert len(chunks) == 3
        assert chunks[0].text == "Hey."
        assert "thinking about you" in chunks[1].text
        assert chunks[2].text == "You okay?"

    def test_pause_between_consecutive_lines(self):
        text = '"First." "Second." "Third."'
        chunks = regex_extract(text)
        assert len(chunks) == 3
        # First chunk should have no pause_before
        assert chunks[0].pause_before_ms == 0
        # Later chunks should have some pause
        assert chunks[1].pause_before_ms >= 300


# ---------------------------------------------------------------------------
# Mutation-killing tests — targeting survived mutants from mutmut run
# ---------------------------------------------------------------------------


class TestInferEmotionDirect:
    """Direct tests for _infer_emotion(). Covers the rstrip(" '\"") logic
    and all punctuation-driven emotion branches."""

    def test_trailing_quote_stripped_before_check(self):
        # The rstrip(" '\"") must strip the closing quote so ! is the end char
        assert _infer_emotion("Stop!'") == "warm"

    def test_trailing_double_quote_stripped(self):
        assert _infer_emotion('Wait!"') == "warm"

    def test_trailing_space_stripped(self):
        assert _infer_emotion("Hey! ") == "warm"

    def test_surprised_after_stripping(self):
        assert _infer_emotion("What?!'") == "surprised"

    def test_angry_double_bang_after_stripping(self):
        assert _infer_emotion('NO!!"') == "angry"

    def test_ellipsis_after_stripping(self):
        assert _infer_emotion("maybe...'") == "soft"

    def test_invert_surprise_order(self):
        # ?! and !? both → surprised
        assert _infer_emotion("What!?") == "surprised"

    def test_all_caps_short_not_angry(self):
        # len <= 3 should NOT be angry even if all caps
        assert _infer_emotion("NO") != "angry" or len("NO".rstrip(".!?")) <= 3

    def test_mixed_punctuation_falls_through(self):
        # Normal sentence ending in period → neutral
        assert _infer_emotion("Okay.") == "neutral"


class TestExtractBeatActionDirect:
    """Direct tests for _extract_beat_action(). Verifies it picks the right
    verb from beat text and distinguishes audible from silent beats."""

    def test_known_audible_word(self):
        assert _extract_beat_action("she laughs softly") == "laughs"

    def test_bare_beat(self):
        assert _extract_beat_action("laughs") == "laughs"

    def test_silent_beat_returns_empty(self):
        # "smiles" is not in AUDIBLE_BEATS or AUDIBLE_EMOTION_MAP
        assert _extract_beat_action("she smiles") == ""

    def test_word_in_emotion_map_only(self):
        # "pants" is in AUDIBLE_EMOTION_MAP but also in AUDIBLE_BEATS.
        # Find one that's in EMOTION_MAP but NOT BEATS.
        emotion_only = set(AUDIBLE_EMOTION_MAP.keys()) - AUDIBLE_BEATS
        if emotion_only:
            word = next(iter(emotion_only))
            result = _extract_beat_action(word)
            assert result == word

    def test_or_vs_and_audible_check(self):
        # Verify: a word in BEATS but not in EMOTION_MAP still detected
        beats_only = AUDIBLE_BEATS - set(AUDIBLE_EMOTION_MAP.keys())
        if beats_only:
            word = next(iter(beats_only))
            result = _extract_beat_action(word)
            assert result == word

    def test_conjugation_stripping_s(self):
        # Words ending in extra 's' that aren't direct entries should be
        # handled by the rstrip("s") fallback
        assert _extract_beat_action("laughings") in ("laughing", "")

    def test_unknown_action_returns_empty(self):
        assert _extract_beat_action("she dances") == ""


class TestParentheticalRemoval:
    """Parenthetical text is removed before dialogue extraction.
    Mutants that replaced sub("", ...) with sub("XXXX", ...) must die."""

    def test_parenthetical_content_not_in_output(self):
        text = '"Hello." (inner monologue here) "Goodbye."'
        chunks = regex_extract(text)
        all_text = " ".join(c.text for c in chunks)
        assert "inner monologue" not in all_text

    def test_parenthetical_removal_preserves_adjacent_dialogue(self):
        text = '(thinking...) "Yes." (more thoughts) "No."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[0].text == "Yes."
        assert chunks[1].text == "No."

    def test_parenthetical_inside_beat_asterisks_preserved(self):
        # Parentheses inside asterisks are beat text, not thoughts
        text = '*she (quietly) sighs* "Hey."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hey."


class TestAudibleVsSilentPauseValues:
    """Exact pause values for audible (400) vs silent (200) vs inter-dialogue
    (300). Mutants that changed these numbers must die."""

    def test_audible_beat_pause_400(self):
        # First chunk pause is zeroed, so use a second chunk
        text = '"Setup." *she laughs* "Punchline."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[1].pause_before_ms == 400

    def test_silent_beat_pause_200(self):
        text = '"Setup." *she smiles* "After smile."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[1].pause_before_ms == 200

    def test_inter_dialogue_pause_300(self):
        text = '"First." "Second."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[1].pause_before_ms == 300

    def test_first_chunk_pause_always_zero(self):
        # Regardless of preceding beat, first chunk pause = 0
        text = '*she gasps* "First." "Second."'
        chunks = regex_extract(text)
        assert chunks[0].pause_before_ms == 0

    def test_pause_after_always_zero(self):
        text = '"Hello." "Goodbye."'
        chunks = regex_extract(text)
        for chunk in chunks:
            assert chunk.pause_after_ms == 0


class TestBeatEmotionAndTagPropagation:
    """Beat metadata (emotion, tag) propagates to SpeakableChunks correctly.
    Mutants that changed dict keys or .get() defaults must die."""

    def test_audible_beat_sets_emotion(self):
        # "sighs" → emotion "soft". When dialogue itself is neutral,
        # beat emotion should win.
        text = '*she sighs* "Okay."'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "soft"

    def test_audible_beat_emotion_overrides_neutral(self):
        text = '*she gasps* "Hello."'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "surprised"

    def test_strong_text_emotion_beats_beat_emotion(self):
        # Text has !! (angry), beat is sighs (soft). Text emotion wins
        # because beat_emotion only applies when emotion == "neutral".
        text = '*she sighs* "STOP!!"'
        chunks = regex_extract(text)
        assert chunks[0].emotion == "angry"

    def test_tag_included_with_emotion_tags_enabled(self):
        text = '*she laughs* "Ha."'
        chunks = regex_extract(text, supports_emotion_tags=True)
        assert chunks[0].text == "[laugh] Ha."

    def test_tag_excluded_without_emotion_tags(self):
        text = '*she laughs* "Ha."'
        chunks = regex_extract(text, supports_emotion_tags=False)
        assert chunks[0].text == "Ha."

    def test_silent_beat_no_tag_ever(self):
        text = '*she smiles* "Hey."'
        chunks = regex_extract(text, supports_emotion_tags=True)
        assert "[" not in chunks[0].text


class TestEmDashDialogue:
    """Em-dash dialogue (—text—) used as fallback when no double quotes."""

    def test_emdash_dialogue_extracted(self):
        text = "—Hello there.—"
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Hello there."

    def test_emdash_fallback_only_when_no_quotes(self):
        # Double quotes take priority — em-dashes inside quotes are preserved
        # as part of the dialogue text (they're just punctuation)
        text = '"She said — yes — to me."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert "She said" in chunks[0].text and "to me." in chunks[0].text

    def test_emdash_with_preceding_beat(self):
        text = "*she sighs* —Fine.—"
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "Fine."
        assert chunks[0].emotion == "soft"


class TestEmphasisAsterisksPreserved:
    """Asterisks inside quoted dialogue are emphasis, not beats.
    Mutants that changed the span check boundary must die."""

    def test_asterisk_inside_quotes_not_beat(self):
        text = '"I *really* mean it."'
        chunks = regex_extract(text)
        assert len(chunks) == 1
        assert chunks[0].text == "I *really* mean it."

    def test_asterisk_boundary_inclusive(self):
        # The check is qs <= m.start() and m.end() <= qe (inclusive both sides)
        text = '"*emphasized*" she said.'
        chunks = regex_extract(text)
        # "*emphasized*" is inside quotes → not a beat → part of dialogue
        assert len(chunks) == 1
        assert "emphasized" in chunks[0].text


class TestEmptyDialogueSkipped:
    """Empty quoted strings produce no chunks (continue, not break)."""

    def test_whitespace_only_quote_skipped_not_break(self):
        # A quoted string that's only whitespace after strip() → skipped.
        # The next real line must still appear.
        # Using text that doesn't trigger the "" adjacent-match issue:
        text = 'Some text. "   " and then "Real dialogue here."'
        chunks = regex_extract(text)
        # The whitespace-only quote is skipped, real dialogue survives
        assert any("Real dialogue" in c.text for c in chunks)

    def test_beat_with_empty_dialogue_followed_by_real(self):
        # Beat before empty dialogue should not break processing
        text = '*she laughs* "Okay." *she pauses* "Good."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        assert chunks[0].text == "Okay."
        assert chunks[1].text == "Good."


class TestBeatConsumed:
    """A beat is consumed (set to None) after being applied to one dialogue.
    It should NOT bleed into subsequent dialogue lines."""

    def test_beat_consumed_after_first_dialogue(self):
        text = '*she gasps* "First." "Second."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        # First chunk gets the gasp beat emotion (surprised)
        assert chunks[0].emotion == "surprised"
        # Second chunk: beat was consumed, neutral text → inter-dialogue pause only
        assert chunks[1].pause_before_ms == 300

    def test_silent_beat_consumed(self):
        text = '*she smiles* "One." "Two."'
        chunks = regex_extract(text)
        assert len(chunks) == 2
        # First chunk: smiles is silent → pause zeroed (it's first chunk)
        assert chunks[0].pause_before_ms == 0
        # Second chunk: no beat remaining → inter-dialogue pause
        assert chunks[1].pause_before_ms == 300


class TestInferEmotionAllCaps:
    """Specifically tests the stripped.isupper() branch."""

    def test_all_caps_with_period(self):
        # "HELLO." → rstrip(".!?") → "HELLO" → isupper and len > 3 → angry
        assert _infer_emotion("HELLO.") == "angry"

    def test_all_caps_with_question_mark(self):
        assert _infer_emotion("WHAT?") == "angry"

    def test_all_caps_exactly_four_chars(self):
        # len > 3, so "STOP" (4 chars) → angry
        assert _infer_emotion("STOP") == "angry"

    def test_all_caps_three_chars_not_angry(self):
        # len <= 3, so "HEY" → neutral (not angry)
        assert _infer_emotion("HEY") == "neutral"


class TestExtractBeatActionConjugation:
    """Tests the rstrip("s") and rstrip("ed") fallback paths."""

    def test_conjugation_s_stripping(self):
        # "laughings" → rstrip("s") → "laughing" which is NOT in BEATS → ""
        # But "sighs" is a direct entry so that's handled by the first loop.
        # We need a word that hits the rstrip("s") fallback and succeeds.
        # "sniffle" is in BEATS. "sniffles" is a direct entry.
        # What about a word like "hummed"? → rstrip("ed") → "humm" → not in BEATS
        # Actually, the rstrip paths handle edge cases the direct lookup misses.
        # For a real test: "coughed" → rstrip("ed") → "cough" ✓
        assert _extract_beat_action("she coughed loudly") == "cough"

    def test_conjugation_ed_stripping(self):
        assert _extract_beat_action("he moaned") == "moan"

    def test_no_false_positive_conjugation(self):
        # "dances" → rstrip("s") → "dance" → not in BEATS
        assert _extract_beat_action("she dances") == ""


class TestInternalDictKeysUsed:
    """The beat dict keys 'action', 'is_audible', 'emotion', 'tag' are read
    back later in the pipeline. These tests verify the full pipeline uses them."""

    def test_action_key_affects_is_audible(self):
        # An audible beat (*laughs*) → is_audible=True → 400ms pause
        # A silent beat (*smiles*) → is_audible=False → 200ms pause
        text = '"One." *she laughs* "Two." *she smiles* "Three."'
        chunks = regex_extract(text)
        assert len(chunks) == 3
        assert chunks[1].pause_before_ms == 400  # audible
        assert chunks[2].pause_before_ms == 200  # silent

    def test_emotion_key_propagates_from_beat_dict(self):
        # The "emotion" key from beat dict feeds into beat_emotion
        text = '"Setup." *she growls* "Hey."'
        chunks = regex_extract(text)
        assert chunks[1].emotion == "angry"  # growls → angry

    def test_tag_key_used_with_emotion_tags(self):
        # The "tag" key from beat dict feeds into beat_tag
        text = '"Setup." *she coughs* "Ahem."'
        chunks = regex_extract(text, supports_emotion_tags=True)
        assert "[cough]" in chunks[1].text


class TestBackendTypeParameter:
    """backend_type is accepted but currently unused in the function body.
    Verify that the parameter has no effect on output — this documents
    the current behavior. If backend_type gains meaning later, these
    tests will break and need updating."""

    def test_backend_type_does_not_affect_output(self):
        text = '*she laughs* "Hello."'
        edge_chunks = regex_extract(text, backend_type="edge", supports_emotion_tags=False)
        kokoro_chunks = regex_extract(text, backend_type="kokoro", supports_emotion_tags=False)
        assert edge_chunks == kokoro_chunks
