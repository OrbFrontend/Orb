"""Conversation identity rules shared by turns, off-turn calls, and estimates.

Every builder of a conversation's prefix resolves the persona, the {{random}} seed, and the card-derived prompt fields through
these functions, so an off-turn prefix cannot drift from the turn's (KV cache prefix parity).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core import Macros, card_description
from ..core.settings import Settings


def resolve_persona_id(conv: Mapping[str, Any], card: Mapping[str, Any] | None, settings: Settings) -> int | None:
    """Return the effective persona id for a turn.

    Priority: conversation pin -> character-card pin -> global active persona.
    """
    return conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")


def conversation_macro_seed(conv: Mapping[str, Any]) -> str:
    """The {{random}} seed for *conv*: its own id, unless a carried
    ``macro_seed`` (set by checkpoint/compress via ``fork_conversation``) pins
    picks to the source conversation so they match the copied history."""
    return conv.get("macro_seed") or conv["id"]


def persona_macros(
    settings: Settings,
    char_name: str,
    persona: Mapping[str, Any] | None,
    seed: str = "",
    card: Mapping[str, Any] | None = None,
    cast: str = "",
) -> tuple[Macros, str]:
    """Build the turn :class:`Macros` plus the resolved user description.

    The description falls back to the global ``user_description`` setting when no persona row is active. *seed*
    (:func:`conversation_macro_seed`) keeps {{random}} in per-turn-resolved prompt fields byte-stable per conversation.

    *card* is the character's, and feeds ``{{description}}``; the returned string is the *user's*. The two are unrelated despite
    the shared word -- one names a card field, the other a persona row's.
    """
    macros = Macros.from_settings(settings, char_name, persona, seed=seed, cast=cast, description=card_description(card))
    user_description = persona.get("description", "") if persona else settings.get("user_description", "")
    return macros, user_description


def char_context(
    settings: Settings, card: Mapping[str, Any] | None, shared_key: str = "shared_system_prompt"
) -> tuple[str, str, str]:
    """Resolve the effective system prompt, persona, and example messages.

    shared_system_prompt and the model-specific system_prompt are concatenated (shared first); a character card's own
    system_prompt, when present and not disabled by the prevent_prompt_overrides setting, replaces that combined result entirely
    rather than appending to it.
    """
    shared = settings.get(shared_key, "")
    model_specific = settings.get("system_prompt", "")
    system_prompt = f"{shared}\n\n{model_specific}" if shared and model_specific else shared or model_specific
    if not card:
        return system_prompt, "", ""
    char_persona = "\n\n".join(filter(None, [card.get("description", ""), card.get("personality", "")]))
    card_system_prompt = card.get("system_prompt")
    if card_system_prompt and not settings.get("prevent_prompt_overrides"):
        system_prompt = card_system_prompt
    return system_prompt, char_persona, card.get("mes_example", "")
