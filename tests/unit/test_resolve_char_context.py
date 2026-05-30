"""resolve_char_context: pre-fetched card reuse and fetch fallback.

Pins that a supplied card short-circuits the internal fetch (so an in-turn
caller's existing read is reused, not duplicated), that the fetch fallback
is preserved when no card is supplied, and that an absent character_card_id
yields empty persona/example without a fetch.
"""

from __future__ import annotations

import backend.database.queries.character_cards as cc


async def test_supplied_card_short_circuits_fetch(monkeypatch):
    async def _fail(*_a, **_k):
        raise AssertionError("get_character_card must not run when a card is supplied")

    monkeypatch.setattr(cc, "get_character_card", _fail)
    card = {"description": "D", "personality": "P", "mes_example": "M", "system_prompt": "S"}
    system_prompt, persona, mes_example = await cc.resolve_char_context({"character_card_id": "ignored"}, {}, card=card)
    assert persona == "D\n\nP"
    assert mes_example == "M"
    assert system_prompt == "S"


async def test_absent_card_id_yields_empty_without_fetch(monkeypatch):
    async def _fail(*_a, **_k):
        raise AssertionError("get_character_card must not run without a character_card_id")

    monkeypatch.setattr(cc, "get_character_card", _fail)
    system_prompt, persona, mes_example = await cc.resolve_char_context({}, {"system_prompt": "base"})
    assert (persona, mes_example) == ("", "")
    assert system_prompt == "base"


async def test_omitted_card_falls_back_to_fetch(monkeypatch):
    async def _fake(card_id):
        assert card_id == "c1"
        return {"description": "FD", "personality": "", "mes_example": "FM"}

    monkeypatch.setattr(cc, "get_character_card", _fake)
    _system_prompt, persona, mes_example = await cc.resolve_char_context({"character_card_id": "c1"}, {})
    assert persona == "FD"
    assert mes_example == "FM"
