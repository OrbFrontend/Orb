"""resolve_persona_id: persona-lock resolution priority.

Pins the priority chain a turn uses to pick its effective persona: conversation lock -> character-card lock -> global active
persona. A locked persona overrides the global active one within its scope; an absent card is tolerated; and nothing-set
resolves to None.
"""

from backend.prompting import resolve_persona_id


def test_conversation_lock_wins_over_character_and_global():
    conv = {"persona_lock_id": 1}
    assert resolve_persona_id(conv, {"persona_lock_id": 2}, {"active_persona_id": 3}) == 1


def test_character_lock_wins_over_global_when_no_conversation_lock():
    conv = {"persona_lock_id": None}
    assert resolve_persona_id(conv, {"persona_lock_id": 2}, {"active_persona_id": 3}) == 2


def test_falls_back_to_global_active_when_no_locks():
    conv = {"persona_lock_id": None}
    assert resolve_persona_id(conv, {"persona_lock_id": None}, {"active_persona_id": 3}) == 3


def test_missing_card_is_tolerated():
    assert resolve_persona_id({"persona_lock_id": None}, None, {"active_persona_id": 3}) == 3


def test_nothing_set_resolves_to_none():
    assert resolve_persona_id({}, None, {}) is None
