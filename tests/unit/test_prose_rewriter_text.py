"""The prompt contract and the four output repairs.

THE PROMPT IS PINNED BYTE-FOR-BYTE because it is a property of the weights, not
a setting: it is the exact string the training pool was written in and the one
each model repo's ``chat_template.jinja`` produces. A hand edit here would not
fail anything, it would quietly serve the model a prompt it has never seen — so
the string is asserted literally rather than rebuilt from the same f-string the
implementation uses, which would agree with any change made to it.

The REPAIRS are pinned against the corpus defect each one exists for, and — as
importantly — against the near-misses they must NOT fire on. An abbreviation is
not a sentence boundary and an emoticon is not punctuation spacing; both are
one careless character class away from being mangled.
"""

from __future__ import annotations

from backend.inference.prose_rewriter import text as T

# ── the prompt ───────────────────────────────────────────────────────────────


def test_serve_prompt_is_the_exact_three_block_string():
    assert T.serve_prompt("The rain fell.") == (
        "<|im_start|>source\nThe rain fell.<|im_end|>\n<|im_start|>edit\nmatch<|im_end|>\n<|im_start|>rewrite\n"
    )


def test_the_edit_block_defaults_to_match():
    """`match` is the band that rewrites in place; serving with no block at all
    measured del:ins 19.0 against 2.8. Nothing should talk this out of it."""
    assert T.EDIT_MODE == "match"
    assert T.serve_prompt("x") == T.serve_prompt("x", T.EDIT_MODE)


def test_an_empty_edit_mode_drops_the_block_entirely():
    assert T.serve_prompt("x", "") == "<|im_start|>source\nx<|im_end|>\n<|im_start|>rewrite\n"


# ── plan: what gets rewritten and what is passed through ─────────────────────

LONG = "A paragraph with more than eighty bytes in it, comfortably past the trained floor."


def test_plan_splits_on_any_newline_run_not_only_blank_lines():
    """The corpus builder split on ``\\n+`` and every training target is one
    such paragraph, so a single newline is a boundary too. Handing the model a
    multi-line block welds the lines together."""
    assert T.plan(f"{LONG}\n{LONG}") == [("rewrite", LONG), ("keep", "\n"), ("rewrite", LONG)]
    assert T.plan(f"{LONG}\n\n{LONG}") == [("rewrite", LONG), ("keep", "\n\n"), ("rewrite", LONG)]


def test_plan_passes_short_paragraphs_through_untouched():
    """Under 80 bytes is outside the training distribution — the corpus dropped
    every human paragraph below it, so the model pads and invents."""
    short = "Short."
    assert len(short.encode()) < T.MIN_REWRITE_BYTES
    assert T.plan(f"{short}\n\n{LONG}") == [("keep", short), ("keep", "\n\n"), ("rewrite", LONG)]


def test_plan_measures_the_floor_in_bytes_not_characters():
    """79 characters of non-ASCII is well over the byte floor the corpus used."""
    wide = "é" * 41  # 82 bytes, 41 characters
    assert len(wide) < T.MIN_REWRITE_BYTES <= len(wide.encode())
    assert T.plan(wide) == [("rewrite", wide)]


def test_plan_keeps_the_separators_so_the_draft_reassembles_whole():
    plan = T.plan(f"{LONG}\n\n\n{LONG}")
    assert "".join(piece for _kind, piece in plan) == f"{LONG}\n\n\n{LONG}"


# ── trim_to_sentence: an unfinished generation ───────────────────────────────


def test_trim_cuts_back_to_the_last_completed_sentence():
    assert T.trim_to_sentence("She left. He stayed for a mom") == "She left."


def test_trim_lands_after_the_closing_quote_not_before_it():
    """Trimming to the '.' inside '..."' would strip the quote and unbalance
    the dialogue this exists to protect."""
    assert T.trim_to_sentence('He said, "Go home." She did not mo') == 'He said, "Go home."'


def test_an_em_dash_ends_a_sentence_only_when_a_quote_closes_it():
    """Interrupted dialogue is a line end; a bare dash between words is a
    parenthetical and must not be cut at."""
    assert T.trim_to_sentence('"What in the—" She spun aro') == '"What in the—"'
    assert T.trim_to_sentence("the plan—all of it—was fall") == ""


def test_trim_returns_empty_when_nothing_ever_ended():
    """The caller falls back to the untrimmed text; silently emptying a
    paragraph would be worse than a ragged tail."""
    assert T.trim_to_sentence("no terminal mark here at all") == ""


# ── normalise_spacing ────────────────────────────────────────────────────────


def test_horizontal_whitespace_collapses_but_paragraphs_survive():
    assert T.normalise_spacing("a   b c") == "a b c"
    assert T.normalise_spacing("one\n\ntwo") == "one\n\ntwo"


def test_exotic_line_breaks_become_ordinary_newlines():
    """Five corpus rows carried U+2028 through the CR/LF cleanup, leaving an
    invisible artefact in the target and then in the model's output.

    Written as escapes, never as the characters themselves: a literal U+2028
    in this file would be invisible in every diff and editor that ever shows
    it, and a test nobody can read is a test nobody can correct.
    """
    assert T.normalise_spacing("one\r\ntwo") == "one\ntwo"
    assert T.normalise_spacing("one\rtwo") == "one\ntwo"
    assert T.normalise_spacing("one\u2028two") == "one\ntwo"
    assert T.normalise_spacing("one\u2029two") == "one\ntwo"
    assert T.normalise_spacing("one\x0btwo") == "one\ntwo"


def test_space_before_punctuation_closes_up():
    assert T.normalise_spacing("Wait , then go ; now") == "Wait, then go; now"


def test_emoticons_and_spaced_ellipses_are_text_not_defects():
    assert T.normalise_spacing("fine :) wink ;)") == "fine :) wink ;)"
    assert T.normalise_spacing("We ... waited") == "We ... waited"


# ── restore_sentence_spacing ─────────────────────────────────────────────────


def test_a_welded_boundary_gets_its_space_back():
    assert T.restore_sentence_spacing("He left.She stayed.") == "He left. She stayed."


def test_abbreviations_and_domains_are_left_alone():
    """`[.!?][a-z]` fires on 207 targets and almost none are boundaries. The
    following capital is the only thing that separates the two."""
    assert T.restore_sentence_spacing("at 4chan.net by 2145 a.d. things") == "at 4chan.net by 2145 a.d. things"


def test_a_closing_quote_keeps_the_space_outside_it():
    assert T.restore_sentence_spacing('"I couldn\'t."Jae sighed.') == '"I couldn\'t." Jae sighed.'


def test_smart_quotes_are_settled_by_the_glyph_alone():
    assert T.restore_sentence_spacing("He stopped.“Go on.") == "He stopped. “Go on."
    assert T.restore_sentence_spacing("“Go on.”He did not.") == "“Go on.” He did not."


def test_back_to_back_dialogue_splits_between_the_two_quotes():
    assert T.restore_sentence_spacing('"Stop.""Never."') == '"Stop." "Never."'


# ── split_lost_paragraphs ────────────────────────────────────────────────────


def test_a_tight_close_open_weld_becomes_the_paragraph_break_it_was():
    """AO3's scrape drops the break, not just the space; the archive's format is
    one paragraph per speaker. The tight form is the lost break (73,224 rows),
    close-space-open is a real single paragraph (3,878)."""
    assert T.split_lost_paragraphs('"I know.""So do I."') == '"I know."\n"So do I."'
    assert T.split_lost_paragraphs('"I know."“So do I."') == '"I know."\n“So do I."'
    assert T.split_lost_paragraphs("“I know.”“So do I.”") == "“I know.”\n“So do I.”"


def test_a_line_end_is_wider_than_a_full_stop_but_excludes_the_comma():
    """`,"` promises a dialogue tag, so it is not a line end."""
    assert T.split_lost_paragraphs('"Wait—""Go."') == '"Wait—"\n"Go."'
    assert T.split_lost_paragraphs('"Wait,""Go."') == '"Wait,""Go."'


def test_quotes_facing_the_wrong_way_are_not_a_turn_boundary():
    assert T.split_lost_paragraphs('"I know."”So do I.') == '"I know."”So do I.'


def test_the_two_repairs_do_not_both_claim_the_same_weld():
    """split_lost_paragraphs runs first; what reaches restore_sentence_spacing's
    back-to-back rule is what the first one declined."""
    assert T.finish('"I know.""So do I."', True) == '"I know."\n"So do I."'


# ── finish ───────────────────────────────────────────────────────────────────


def test_finish_trims_only_when_the_model_ran_out_of_budget():
    assert T.finish("He left. She sta", stopped=False) == "He left."
    assert T.finish("He left. She sta", stopped=True) == "He left. She sta"


def test_an_unstopped_generation_with_no_finished_sentence_is_kept_whole():
    """`trim_to_sentence() or text` — emptying the paragraph would be worse."""
    assert T.finish("one long unfinished clause", stopped=False) == "one long unfinished clause"


def test_finish_applies_the_repairs_and_strips():
    assert T.finish("  He left.She stayed.  ", stopped=True) == "He left. She stayed."
