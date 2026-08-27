"""The prompt contract and the output repairs.

THE PROMPT IS PINNED BYTE-FOR-BYTE because it is a property of the weights, not
a setting. It is asserted literally rather than rebuilt from the implementation's
own f-string, which would agree with any change made to it.

The REPAIRS are pinned against the corpus defect each one exists for and — as
importantly — against the near-misses they must NOT fire on: an abbreviation is
not a sentence boundary and an emoticon is not punctuation spacing.
"""

from __future__ import annotations

from backend.inference.prose_rewriter import text as T

# ── the prompt ───────────────────────────────────────────────────────────────


def test_serve_prompt_is_the_exact_three_block_string():
    assert T.serve_prompt("The rain fell.") == (
        "<|im_start|>source\nThe rain fell.<|im_end|>\n<|im_start|>edit\nmatch<|im_end|>\n<|im_start|>rewrite\n"
    )


# ── plan: what gets rewritten and what is passed through ─────────────────────

LONG = "A paragraph with more than eighty bytes in it, comfortably past the trained floor."


def test_plan_splits_on_any_newline_run_not_only_blank_lines():
    """The corpus builder split on ``\\n+``, so a single newline is a boundary
    too — a multi-line block welds the lines together."""
    assert T.plan(f"{LONG}\n{LONG}") == [("rewrite", LONG), ("keep", "\n"), ("rewrite", LONG)]
    assert T.plan(f"{LONG}\n\n{LONG}") == [("rewrite", LONG), ("keep", "\n\n"), ("rewrite", LONG)]


def test_plan_passes_short_paragraphs_through_untouched():
    """Under 80 bytes is outside the training distribution; the model pads and
    invents. The floor is bytes, not characters."""
    short = "Short."
    assert T.plan(f"{short}\n\n{LONG}") == [("keep", short), ("keep", "\n\n"), ("rewrite", LONG)]
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
    """Trimming to the '.' inside '..."' would unbalance the dialogue this
    exists to protect."""
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
    assert T.normalise_spacing("a   b c") == "a b c"
    assert T.normalise_spacing("one\n\ntwo") == "one\n\ntwo"


def test_exotic_line_breaks_become_ordinary_newlines():
    """Written as escapes, never as the characters themselves: a literal U+2028
    here would be invisible in every diff that ever shows it."""
    assert T.normalise_spacing("one\r\ntwo") == "one\ntwo"
    assert T.normalise_spacing("one\rtwo") == "one\ntwo"
    assert T.normalise_spacing("one\u2028two") == "one\ntwo"
    assert T.normalise_spacing("one\u2029two") == "one\ntwo"
    assert T.normalise_spacing("one\x0btwo") == "one\ntwo"


def test_space_before_punctuation_closes_up_but_spares_emoticons():
    assert T.normalise_spacing("Wait , then go ; now") == "Wait, then go; now"
    assert T.normalise_spacing("fine :) wink ;)") == "fine :) wink ;)"
    assert T.normalise_spacing("We ... waited") == "We ... waited"


# ── restore_sentence_spacing ─────────────────────────────────────────────────


def test_a_welded_boundary_gets_its_space_back_but_abbreviations_do_not():
    """`[.!?][a-z]` fires on 207 targets and almost none are boundaries. The
    following capital is the only thing that separates the two."""
    assert T.restore_sentence_spacing("He left.She stayed.") == "He left. She stayed."
    assert T.restore_sentence_spacing("at 4chan.net by 2145 a.d. things") == "at 4chan.net by 2145 a.d. things"


def test_the_space_lands_outside_the_quote_it_belongs_to():
    assert T.restore_sentence_spacing('"I couldn\'t."Jae sighed.') == '"I couldn\'t." Jae sighed.'
    assert T.restore_sentence_spacing("He stopped.“Go on.") == "He stopped. “Go on."
    assert T.restore_sentence_spacing("“Go on.”He did not.") == "“Go on.” He did not."
    assert T.restore_sentence_spacing('"Stop.""Never."') == '"Stop." "Never."'


# ── split_lost_paragraphs ────────────────────────────────────────────────────


def test_a_tight_close_open_weld_becomes_the_paragraph_break_it_was():
    """AO3's scrape drops the break, not just the space. The tight form is the
    lost break (73,224 rows); close-space-open is a real paragraph (3,878)."""
    assert T.split_lost_paragraphs('"I know.""So do I."') == '"I know."\n"So do I."'
    assert T.split_lost_paragraphs('"I know."“So do I."') == '"I know."\n“So do I."'
    assert T.split_lost_paragraphs("“I know.”“So do I.”") == "“I know.”\n“So do I.”"


def test_a_line_end_is_wider_than_a_full_stop_but_excludes_the_comma():
    """`,"` promises a dialogue tag, so it is not a line end. Quotes facing the
    wrong way are not a turn boundary at all."""
    assert T.split_lost_paragraphs('"Wait—""Go."') == '"Wait—"\n"Go."'
    assert T.split_lost_paragraphs('"Wait,""Go."') == '"Wait,""Go."'
    assert T.split_lost_paragraphs('"I know."”So do I.') == '"I know."”So do I.'


# ── finish ───────────────────────────────────────────────────────────────────


def test_finish_trims_only_when_the_model_ran_out_of_budget():
    assert T.finish("He left. She sta", stopped=False) == "He left."
    assert T.finish("He left. She sta", stopped=True) == "He left. She sta"


def test_an_unstopped_generation_with_no_finished_sentence_is_kept_whole():
    """`trim_to_sentence() or text` — emptying the paragraph would be worse."""
    assert T.finish("one long unfinished clause", stopped=False) == "one long unfinished clause"


def test_finish_applies_the_repairs_in_order_and_strips():
    """split_lost_paragraphs runs before restore_sentence_spacing, so the two
    never both claim the same weld."""
    assert T.finish("  He left.She stayed.  ", stopped=True) == "He left. She stayed."
    assert T.finish('"I know.""So do I."', True) == '"I know."\n"So do I."'
