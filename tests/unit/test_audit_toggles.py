"""
test_audit_toggles.py — verify run_audit honours the per-scanner toggle map,
skipping disabled scanners while leaving enabled ones intact.
"""

from __future__ import annotations

from backend.passes.editor.audit import AUDIT_TYPES, run_audit


# Banned phrase that detect_cliches will flag (matches a seeded literal group).
_PHRASE_BANK = [["tension in the air"]]
_BANNED_TEXT = "The tension in the air was palpable. The tension in the air grew."

# Two near-identical messages trigger phrase + structural repetition.
_PREV_MSGS = ["She walked to the door and opened it slowly."]
_REPEAT_DRAFT = "She walked to the door and opened it slowly."


def test_default_runs_all_scanners():
    report = run_audit(_BANNED_TEXT, _PHRASE_BANK)
    assert report.cliche_result.flagged_count > 0


def test_banned_phrases_toggle_off_skips_scanner():
    toggles = {t: True for t in AUDIT_TYPES}
    toggles["banned_phrases"] = False
    report = run_audit(_BANNED_TEXT, _PHRASE_BANK, audit_toggles=toggles)
    assert report.cliche_result.flagged_count == 0


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
    off = run_audit(
        full,
        [],
        assistant_messages=_PREV_MSGS,
        structural_text=_REPEAT_DRAFT,
        audit_toggles=toggles,
    )
    assert off.phrase_result is None
    assert off.structural_repetition_result is None
