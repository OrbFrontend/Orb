"""Keyword-scan matcher: V3 `use_regex` + `selective`/`secondary_keys`.

The scan feeds the *trailing* block only — constant entries ride the cached
system prefix, so this path must never change which constants are selected
(KV-cache prefix parity; see test_constant_lorebook_prefix.py for the seam).
"""

from __future__ import annotations

from backend.prompting.lorebook import select_active_entries, select_keyword_entries


def _entry(**kw):
    base = {"id": 1, "name": "E", "content": "c", "keywords": ["doom"], "case_insensitive": True, "constant": 0}
    return {**base, **kw}


def _msgs(text):
    return [{"content": text}]


def test_plain_substring_unchanged():
    assert select_keyword_entries(_msgs("enter Doom now"), [_entry()])
    assert not select_keyword_entries(_msgs("nobody here"), [_entry()])


def test_regex_keyword_matches():
    e = _entry(keywords=[r"doo?m\b"], use_regex=1)
    assert select_keyword_entries(_msgs("enter dom now"), [e])
    assert not select_keyword_entries(_msgs("domain name"), [e])


def test_regex_respects_case_sensitivity():
    e = _entry(keywords=["^Doom"], use_regex=1, case_insensitive=0)
    assert select_keyword_entries(_msgs("Doom arrives"), [e])
    assert not select_keyword_entries(_msgs("doom arrives"), [e])


def test_invalid_pattern_degrades_to_substring_instead_of_raising():
    e = _entry(keywords=["*star"], use_regex=1)
    assert select_keyword_entries(_msgs("a *star fell"), [e])
    assert not select_keyword_entries(_msgs("a planet fell"), [e])


def test_selective_requires_a_secondary_hit():
    e = _entry(selective=1, secondary_keys=["latveria"])
    assert not select_keyword_entries(_msgs("doom alone"), [e])
    assert select_keyword_entries(_msgs("doom rules latveria"), [e])


def test_selective_with_empty_secondary_keys_does_not_gate():
    assert select_keyword_entries(_msgs("doom alone"), [_entry(selective=1, secondary_keys=[])])


def test_constant_entries_are_still_excluded_from_the_trailing_block():
    """Prefix parity: constants never come back from the per-turn selection,
    whatever the new flags say."""
    const = _entry(id=2, name="Canon", keywords=["doom"], constant=1, use_regex=1, selective=1)
    assert select_active_entries([const, _entry()], _msgs("doom"), scan_depth=6) == [_entry()]
