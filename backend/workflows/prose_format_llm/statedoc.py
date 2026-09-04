"""Pure helpers over the per-conversation state document.

State shape: ``{"schema": {elem: description}, "values": {elem: denotation},
"auto_analyzed": bool}``. ``schema`` guides the analyzer; ``values`` is the only
thing the judge and enforcer read; ``auto_analyzed`` records that the one
automatic analysis attempt has fired.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import DEFAULT_SCHEMA


def seed() -> dict:
    """A fresh, unarmed state: schema defaults, no recorded values, auto attempt pending."""
    return {"schema": dict(DEFAULT_SCHEMA), "values": {}, "auto_analyzed": False}


def filled_elements(state: Mapping[str, Any] | None) -> dict[str, str]:
    """The elements the judge/enforcer act on: those with a non-empty string value.

    The ``isinstance`` guard is defensive -- the analyzer and ``save`` only ever
    write strings, but a malformed LLM reply or a hand-edited slot must not make
    this (or any caller, e.g. the menu's ``get``) raise.
    """
    values = state.get("values") if isinstance(state, Mapping) else None
    if not isinstance(values, Mapping):
        return {}
    return {k: v for k, v in values.items() if isinstance(v, str) and v.strip()}


def is_armed(state: Mapping[str, Any] | None) -> bool:
    """True once at least one element has a recorded value; the loop runs only when armed."""
    return bool(filled_elements(state))
