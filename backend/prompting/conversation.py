"""Conversation identity rules shared by turns, off-turn calls, and estimates.

Every builder of a conversation's prefix resolves the persona, the {{random}} seed, and the card-derived prompt fields through
these functions, so an off-turn prefix cannot drift from the turn's (KV cache prefix parity).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from ..core import Macros, card_description, card_personality, fill_original
from ..core.prompt_layout import LayoutBlock, main_prompt_text
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

    *card* is the character's, and feeds ``{{description}}`` and ``{{personality}}``; the returned string is the *user's*. The two are unrelated despite
    the shared word -- one names a card field, the other a persona row's.
    """
    macros = Macros.from_settings(
        settings,
        char_name,
        persona,
        seed=seed,
        cast=cast,
        description=card_description(card),
        personality=card_personality(card),
    )
    user_description = persona.get("description", "") if persona else settings.get("user_description", "")
    return macros, user_description


class CharContext(NamedTuple):
    """The card-derived prompt fields, unresolved."""

    system_prompt: str
    description: str
    personality: str
    mes_example: str


def char_context(settings: Settings, card: Mapping[str, Any] | None, layout: Sequence[LayoutBlock]) -> CharContext:
    """Resolve the effective main prompt and the card's description, personality, and example messages.

    The main prompt is *layout*'s Main prompt text; a character card's own system_prompt, when present and not disabled by the
    prevent_prompt_overrides setting, replaces it, a ``{{original}}`` in it standing for the Main prompt it replaces.
    """
    system_prompt = main_prompt_text(layout)
    if not card:
        return CharContext(system_prompt, "", "", "")
    card_system_prompt = card.get("system_prompt")
    if card_system_prompt and not settings.get("prevent_prompt_overrides"):
        system_prompt = fill_original(card_system_prompt, system_prompt)
    return CharContext(
        system_prompt, card.get("description") or "", card.get("personality") or "", card.get("mes_example") or ""
    )


def chat_instructions(settings: Settings, conv: Mapping[str, Any]) -> str:
    """The chat's own instruction text, unless prompt overrides are off; ``{{original}}`` in it has no global text to stand for."""
    if settings.get("prevent_prompt_overrides"):
        return ""
    return fill_original(str(conv.get("post_history_instructions", "") or ""), "")
