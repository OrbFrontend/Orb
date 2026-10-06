"""test_audit_toggles.py -- verify run_audit honours the per-scanner toggle map, skipping disabled scanners while leaving enabled
ones intact.
"""

from backend.analysis import AUDIT_TYPES, run_audit

# Banned phrase that detect_cliches will flag (matches a seeded literal group).
_PHRASE_BANK = [["tension in the air"]]
_BANNED_TEXT = "The tension in the air was palpable. The tension in the air grew."

# Two near-identical messages trigger phrase + structural repetition.
_PREV_MSGS = ["She walked to the door and opened it slowly."]
_REPEAT_DRAFT = "She walked to the door and opened it slowly."

# A draft that parrots the user's spoken dialogue back as a question.
_ECHO_USER = '"I have absolutely no money."'
_ECHO_DRAFT = '"Absolutely no money?" she repeats.'


def test_default_runs_all_scanners():
    assert run_audit(_BANNED_TEXT, _PHRASE_BANK).cliche_result.flagged_count > 0


def test_banned_phrases_toggle_off_skips_scanner():
    toggles = {t: True for t in AUDIT_TYPES}
    toggles["banned_phrases"] = False
    assert run_audit(_BANNED_TEXT, _PHRASE_BANK, audit_toggles=toggles).cliche_result.flagged_count == 0


def test_none_toggles_is_all_on():
    # None must preserve legacy all-on behaviour for older databases.
    assert run_audit(_BANNED_TEXT, _PHRASE_BANK, audit_toggles=None).cliche_result.flagged_count > 0


def test_cross_message_toggles_off_skips_scanners():
    full = "\n\n".join(_PREV_MSGS + [_REPEAT_DRAFT])
    on = run_audit(full, [], assistant_messages=_PREV_MSGS, structural_text=_REPEAT_DRAFT)
    assert on.phrase_result is not None
    assert on.structural_repetition_result is not None

    toggles = {t: True for t in AUDIT_TYPES}
    toggles["phrase_repetition"] = False
    toggles["structural_repetition"] = False
    off = run_audit(full, [], assistant_messages=_PREV_MSGS, structural_text=_REPEAT_DRAFT, audit_toggles=toggles)
    assert off.phrase_result is None
    assert off.structural_repetition_result is None


def test_anti_echo_runs_with_user_message():
    report = run_audit(_ECHO_DRAFT, [], user_message=_ECHO_USER)
    assert report.echo_result is not None
    assert len(report.echo_result.flagged_echoes) > 0


def test_anti_echo_toggle_off_skips_scanner():
    toggles = {t: True for t in AUDIT_TYPES}
    toggles["anti_echo"] = False
    assert run_audit(_ECHO_DRAFT, [], user_message=_ECHO_USER, audit_toggles=toggles).echo_result is None


def test_anti_echo_skipped_without_user_message():
    # No user message -> nothing to compare against, scanner is a no-op.
    assert run_audit(_ECHO_DRAFT, []).echo_result is None
