"""Tests for slop_detector -- detect_cliches and format_report integration.

Regression: the report must show the exact matched phrase from the text, not a canonical/representative form from the variant
group. E.g. if the phrase bank has ["a dance of", "dancing"] and the text contains "dancing", the report must say "dancing", not
"a dance of".
"""

import pytest

from backend.analysis import format_report, run_audit
from backend.analysis.detectors.slop_detector import detect_cliches

# ===============================================================================
# ClicheHit.phrase -- always reflects what was found in the text
# ===============================================================================


class TestMatchedPhrase:
    @pytest.mark.parametrize(
        ("phrase_bank", "text", "phrase"),
        [
            # Single-word variant that is not first in the group.
            ([["a dance of", "dancing"]], "She lets out a short laugh, her eyes dancing with amusement.", "dancing"),
            # The first variant itself matches.
            ([["a dance of", "dancing"]], "It was a dance of shadows and light.", "a dance of"),
            # 2-token non-first variant.
            ([["heart racing", "pulse quickening"]], "Her pulse quickening, she reached for the door.", "pulse quickening"),
            # A literal group in dict form behaves like the legacy list form.
            ([{"kind": "literal", "variants": ["a mix of", "a mixture of"]}], "It was a mixture of styles.", "a mixture of"),
        ],
        ids=["single_word_non_first_variant", "first_variant_matched", "two_token_non_first_variant", "literal_dict_shape"],
    )
    def test_phrase_reflects_the_matched_variant(self, phrase_bank, text, phrase):
        result = detect_cliches(text, phrase_bank)
        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == phrase

    def test_long_phrase_non_first_variant(self):
        """4+ token non-first variant (trigram path) is stored correctly."""
        phrase_bank = [["tension in the air", "the air is thick with tension"]]
        result = detect_cliches("The air is thick with tension as they face each other.", phrase_bank, threshold=0.4)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "the air is thick with tension"

    def test_unique_cliches_uses_matched_phrases(self):
        """unique_cliches lists the phrases that actually appeared, not group representatives."""
        phrase_bank = [["a dance of", "dancing"]]
        text = "She lets out a short laugh, her eyes dancing with amusement."
        assert detect_cliches(text, phrase_bank).unique_cliches == ["dancing"]


# ===============================================================================
# format_report -- displays the matched phrase
# ===============================================================================


class TestFormatReportPhrase:
    def test_report_shows_matched_phrase_not_group_representative(self):
        """Regression: the formatted report must show what was in the text."""
        phrase_bank = [["a dance of", "dancing"]]
        report_text = format_report(run_audit("She lets out a short laugh, her eyes dancing with amusement.", phrase_bank))

        assert '"dancing"' in report_text
        assert '"a dance of"' not in report_text

    def test_report_shows_first_variant_when_it_matched(self):
        """When the first variant matched, the report shows it correctly."""
        phrase_bank = [["a dance of", "dancing"]]
        assert '"a dance of"' in format_report(run_audit("It was a dance of shadows and light.", phrase_bank))


# ===============================================================================
# Regex groups -- {"kind": "regex", "pattern": ...}
# ===============================================================================


class TestRegexGroups:
    def test_alternation_matches_and_reports_matched_text(self):
        """A regex group flags the sentence and reports the matched substring."""
        phrase_bank = [{"kind": "regex", "pattern": r"the air (is|was) (thick|heavy|charged)"}]
        result = detect_cliches("The air was thick between them.", phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "The air was thick"
        assert result.unique_cliches == ["The air was thick"]

    def test_case_insensitive(self):
        phrase_bank = [{"kind": "regex", "pattern": r"\bvoid\b"}]
        result = detect_cliches("A VOID opened beneath her.", phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "VOID"

    def test_word_boundary_avoids_substring(self):
        phrase_bank = [{"kind": "regex", "pattern": r"\bcat\b"}]
        assert detect_cliches("The category was vague.", phrase_bank).flagged_count == 0

    def test_flexible_spacing(self):
        phrase_bank = [{"kind": "regex", "pattern": r"heart\s+racing"}]
        assert detect_cliches("Her heart   racing, she ran.", phrase_bank).flagged_count == 1

    def test_invalid_pattern_is_skipped_not_raised(self):
        """A malformed pattern must not abort the audit; it is silently skipped."""
        phrase_bank = [{"kind": "regex", "pattern": r"(unclosed"}, ["a mix of"]]
        result = detect_cliches("It was a mix of things.", phrase_bank)

        # The valid literal group still fires.
        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "a mix of"

    def test_regex_report_shows_matched_text(self):
        phrase_bank = [{"kind": "regex", "pattern": r"the air (is|was) (thick|heavy)"}]
        assert '"The air is heavy"' in format_report(run_audit("The air is heavy with smoke.", phrase_bank))


# ===============================================================================
# Single-sentence containment -- a match never spans a sentence boundary
# ===============================================================================


class TestSingleSentenceContainment:
    def test_greedy_pattern_does_not_cross_sentence_split(self):
        """A `.*` pattern cannot match across a normal sentence boundary."""
        phrase_bank = [{"kind": "regex", "pattern": r"the air.*thick"}]
        assert detect_cliches("She breathed the air. The soup was thick.", phrase_bank).flagged_count == 0

    def test_greedy_pattern_matches_within_one_sentence(self):
        phrase_bank = [{"kind": "regex", "pattern": r"the air.*thick"}]
        result = detect_cliches("The air grew thick with smoke.", phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "The air grew thick"

    def test_match_rejected_when_it_bridges_an_undersplit_boundary(self):
        """A no-space boundary ("clear.The") leaves the chunk un-split, but a
        match that bridges it is still rejected by the boundary guard."""
        phrase_bank = [{"kind": "regex", "pattern": r"air.*thick"}]
        assert detect_cliches("The air was clear.The fog was thick.", phrase_bank).flagged_count == 0


# ===============================================================================
# Dialogue/narration separation -- a flagged snippet never mixes the two
# ===============================================================================


class TestDialogueNarrationSeparation:
    def test_hit_in_attribution_tail_excludes_dialogue(self):
        """A banned phrase in the attribution after a quote flags only the
        narration fragment, not the quoted speech (regression: the editor was
        rewriting dialogue cited in the audit report)."""
        phrase_bank = [{"kind": "regex", "pattern": r"voice\W+(\w+\W+){0,2}(low|dangerous|dropping)"}]
        result = detect_cliches('"You\'ve got some nerve, Kit," she says, her voice dropping an octave.', phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].sentence == "she says, her voice dropping an octave."

    def test_hit_inside_dialogue_excludes_narration(self):
        """A banned phrase inside the quote flags only the quoted segment."""
        phrase_bank = [["don't you dare"]]
        result = detect_cliches('"Don\'t you dare," she whispered, stepping closer.', phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].sentence == '"Don\'t you dare,"'

    def test_flagged_segment_is_substring_of_source(self):
        """Reported snippets stay contiguous substrings of the draft -- the
        editor's flagged-sentence filter and search/replace depend on it."""
        phrase_bank = [{"kind": "regex", "pattern": r"voice\W+(\w+\W+){0,2}dropping"}, ["barely a whisper"]]
        text = '"Stop right there," he warned, his voice dropping low.\n\nIt was barely a whisper. *He knew.* "Fine."'
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 2
        for fs in result.flagged_sentences:
            assert fs.sentence in text
            assert not (fs.sentence.count('"') == 1)  # no half-quoted snippets
