"""
Tests for slop_detector — detect_cliches and format_report integration.

Regression: the report must show the exact matched phrase from the text, not a
canonical/representative form from the variant group.  E.g. if the phrase bank
has ["a dance of", "dancing"] and the text contains "dancing", the report must
say "dancing", not "a dance of".
"""

from __future__ import annotations

from backend.passes.editor.audit import format_report, run_audit
from backend.passes.editor.slop_detector import detect_cliches

# ═══════════════════════════════════════════════════════════════════════════════
# ClicheHit.phrase — always reflects what was found in the text
# ═══════════════════════════════════════════════════════════════════════════════


class TestMatchedPhrase:
    def test_single_word_non_first_variant(self):
        """Single-word variant that is not first in the group stores that word."""
        phrase_bank = [["a dance of", "dancing"]]
        text = "She lets out a short laugh, her eyes dancing with amusement."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "dancing"

    def test_first_variant_matched(self):
        """When the first variant itself matches, phrase equals that variant."""
        phrase_bank = [["a dance of", "dancing"]]
        text = "It was a dance of shadows and light."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "a dance of"

    def test_two_token_non_first_variant(self):
        """2-token non-first variant is stored correctly."""
        phrase_bank = [["heart racing", "pulse quickening"]]
        text = "Her pulse quickening, she reached for the door."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "pulse quickening"

    def test_long_phrase_non_first_variant(self):
        """4+ token non-first variant (trigram path) is stored correctly."""
        phrase_bank = [["tension in the air", "the air is thick with tension"]]
        text = "The air is thick with tension as they face each other."
        result = detect_cliches(text, phrase_bank, threshold=0.4)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "the air is thick with tension"

    def test_unique_cliches_uses_matched_phrases(self):
        """unique_cliches lists the phrases that actually appeared, not group representatives."""
        phrase_bank = [["a dance of", "dancing"]]
        text = "She lets out a short laugh, her eyes dancing with amusement."
        result = detect_cliches(text, phrase_bank)

        assert result.unique_cliches == ["dancing"]


# ═══════════════════════════════════════════════════════════════════════════════
# format_report — displays the matched phrase
# ═══════════════════════════════════════════════════════════════════════════════


class TestFormatReportPhrase:
    def test_report_shows_matched_phrase_not_group_representative(self):
        """Regression: the formatted report must show what was in the text."""
        phrase_bank = [["a dance of", "dancing"]]
        text = "She lets out a short laugh, her eyes dancing with amusement."
        report_text = format_report(run_audit(text, phrase_bank))

        assert '"dancing"' in report_text
        assert '"a dance of"' not in report_text

    def test_report_shows_first_variant_when_it_matched(self):
        """When the first variant matched, the report shows it correctly."""
        phrase_bank = [["a dance of", "dancing"]]
        text = "It was a dance of shadows and light."
        report_text = format_report(run_audit(text, phrase_bank))

        assert '"a dance of"' in report_text


# ═══════════════════════════════════════════════════════════════════════════════
# Regex groups — {"kind": "regex", "pattern": ...}
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegexGroups:
    def test_alternation_matches_and_reports_matched_text(self):
        """A regex group flags the sentence and reports the matched substring."""
        phrase_bank = [{"kind": "regex", "pattern": r"the air (is|was) (thick|heavy|charged)"}]
        text = "The air was thick between them."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "The air was thick"
        assert result.unique_cliches == ["The air was thick"]

    def test_case_insensitive(self):
        phrase_bank = [{"kind": "regex", "pattern": r"\bvoid\b"}]
        text = "A VOID opened beneath her."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "VOID"

    def test_word_boundary_avoids_substring(self):
        phrase_bank = [{"kind": "regex", "pattern": r"\bcat\b"}]
        text = "The category was vague."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 0

    def test_flexible_spacing(self):
        phrase_bank = [{"kind": "regex", "pattern": r"heart\s+racing"}]
        text = "Her heart   racing, she ran."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1

    def test_invalid_pattern_is_skipped_not_raised(self):
        """A malformed pattern must not abort the audit; it is silently skipped."""
        phrase_bank = [
            {"kind": "regex", "pattern": r"(unclosed"},
            ["a mix of"],
        ]
        text = "It was a mix of things."
        result = detect_cliches(text, phrase_bank)

        # The valid literal group still fires.
        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "a mix of"

    def test_literal_dict_shape_still_matches(self):
        """A literal group expressed as a dict behaves like the legacy list form."""
        phrase_bank = [{"kind": "literal", "variants": ["a mix of", "a mixture of"]}]
        text = "It was a mixture of styles."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "a mixture of"

    def test_regex_report_shows_matched_text(self):
        phrase_bank = [{"kind": "regex", "pattern": r"the air (is|was) (thick|heavy)"}]
        text = "The air is heavy with smoke."
        report_text = format_report(run_audit(text, phrase_bank))

        assert '"The air is heavy"' in report_text


# ═══════════════════════════════════════════════════════════════════════════════
# Single-sentence containment — a match never spans a sentence boundary
# ═══════════════════════════════════════════════════════════════════════════════


class TestSingleSentenceContainment:
    def test_greedy_pattern_does_not_cross_sentence_split(self):
        """A `.*` pattern cannot match across a normal sentence boundary."""
        phrase_bank = [{"kind": "regex", "pattern": r"the air.*thick"}]
        text = "She breathed the air. The soup was thick."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 0

    def test_greedy_pattern_matches_within_one_sentence(self):
        phrase_bank = [{"kind": "regex", "pattern": r"the air.*thick"}]
        text = "The air grew thick with smoke."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 1
        assert result.flagged_sentences[0].cliches[0].phrase == "The air grew thick"

    def test_match_rejected_when_it_bridges_an_undersplit_boundary(self):
        """A no-space boundary ("clear.The") leaves the chunk un-split, but a
        match that bridges it is still rejected by the boundary guard."""
        phrase_bank = [{"kind": "regex", "pattern": r"air.*thick"}]
        text = "The air was clear.The fog was thick."
        result = detect_cliches(text, phrase_bank)

        assert result.flagged_count == 0
