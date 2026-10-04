"""Estimate the size of a conversation's next Writer call, broken down by prompt component."""

from __future__ import annotations

from typing import Any

from .. import database as db
from ..core import CardScripts, estimate_tokens, state_fragments_of
from ..database.models import ConversationRow
from ..prompting import (
    char_context,
    compute_style_injection_block,
    conversation_macro_seed,
    group_context,
    macro_identity,
    persona_macros,
    render_history,
    render_state_block,
    resolve_mood_fragment_randoms,
)
from ..prompting.lorebook import compute_constant_lorebook_block
from .context import build_lorebook_turn, resolve_card_and_persona
from .predicates import agent_enabled


async def estimate_context_size(conv: ConversationRow) -> dict[str, Any]:
    """Measure each component of the Writer prompt a turn in *conv* would send now.

    Reads the same rows and applies the same merges as turn loading, but builds no model client, so a conversation without a
    configured endpoint still gets an estimate. A group's estimate is a *maximum* call, not a sum.
    """
    cid = conv["id"]
    settings = await db.get_settings()
    # Only content is measured; the attachments' bytes would be megabytes of
    # base64 read and discarded on every repaint's estimate.
    messages = await db.get_active_path(cid)
    director = await db.get_director_state(cid) or {}

    card, active_persona = await resolve_card_and_persona(conv, settings)
    turn_cast = await db.resolve_cast(conv)
    # The same reader and the same merge the turn uses (globals win on id
    # collision), so the estimate cannot bill a different fragment set.
    card_moods, card_interactive, _card_sources = await db.cast_embedded_fragments(card, turn_cast)
    director_frags = db.merge_fragments_by_id(
        [f for f in await db.get_interactive_fragments() if f.get("enabled", True)], card_interactive
    )
    mood_frags = db.merge_fragments_by_id([f for f in await db.get_mood_fragments() if f.get("enabled", True)], card_moods)
    lorebook_entries = await db.get_active_lorebook_entries(await db.get_effective_world_ids(cid))
    macro_char, cast_names = macro_identity(conv, turn_cast)
    macros, user_desc = persona_macros(
        settings, macro_char, active_persona, seed=conversation_macro_seed(conv), card=card, cast=cast_names
    )
    system_prompt, char_persona, mes_example = char_context(settings, card)

    persona_text = macros.resolve_message(char_persona)
    scenario_text = macros.resolve_message(conv.get("character_scenario", "") or "")
    mes_text = macros.resolve_message(mes_example or "")
    post_text = macros.resolve_message(
        "" if settings.get("prevent_prompt_overrides") else (conv.get("post_history_instructions", "") or "")
    )
    # The group breakdown follows the context mode: the shared body once, plus the largest single speaker's share of it,
    # rendered through the same projection the prompt uses.
    group_components: list[tuple[str, str]] = []
    if turn_cast.grouped:
        # The card-derived halves are replaced by the group components below; `post_text` is the *scene's* directive, which
        # `build_prefix` still renders into the shared body for a group, so it keeps being billed.
        persona_text = ""
        mes_text = ""
        group_components = group_context.context_size_components(
            turn_cast, macros, prevent_prompt_overrides=bool(settings.get("prevent_prompt_overrides"))
        )
    resolved_user_desc = macros.resolve_message(user_desc)
    user_persona_text = f"## User: {macros.user}\n{resolved_user_desc}" if resolved_user_desc.strip() else ""
    # Project the history through the same macros, card scripts, and group labels used by the model-facing prefix.
    history = render_history(
        messages,
        macros,
        cast=turn_cast,
        speaker_names=await db.get_speaker_names(cid) if turn_cast.grouped else None,
        scripts=CardScripts.from_extensions(card.get("extensions") if card else None),
        speaker_scripts=await db.get_group_member_scripts(cid) if turn_cast.grouped else None,
    )
    msg_chars = 0
    for message in history:
        content = message["content"]
        if isinstance(content, str):
            msg_chars += len(content)
        else:
            msg_chars += sum(len(part["text"]) for part in content if part["type"] == "text")

    # Director injection -- fragment {{random}} resolves against a throwaway copy of the stored choice map so the estimate
    # matches the prompt bytes a real turn would inject, without recording new picks.
    active_moods = director.get("active_moods", [])
    est_mood_frags = resolve_mood_fragment_randoms(mood_frags, active_moods, dict(director.get("macro_choices", {})))
    inj_block = compute_style_injection_block(
        active_moods, active_moods, est_mood_frags, director_frags, agent_enabled(settings), {}
    )
    # The Writer's current-state block rides the same injection on every turn.
    writer_state = [fragment for fragment in state_fragments_of(director_frags) if fragment.injects_writer]
    if writer_state:
        state_block = render_state_block(writer_state, await db.fold_path_state(cid, [m["id"] for m in messages]))
        if state_block:
            inj_block = (inj_block + "\n\n" + macros.resolve_message(state_block)).strip()

    # The Writer's lorebook block as the turn builds it; with agentic lorebook on, the part known before the Director picks.
    lorebook = build_lorebook_turn(settings, lorebook_entries, messages, macros)

    components = [
        ("system_prompt", len(system_prompt or "")),
        ("char_persona", len(persona_text)),
        ("scenario", len(scenario_text)),
        ("mes_example", len(mes_text)),
        ("user_persona", len(user_persona_text)),
        ("messages", msg_chars),
        ("post_history", len(post_text)),
        ("director_injection", len(inj_block)),
        ("lorebook", len(lorebook.writer_block((), macros))),
        ("lorebook_constant", len(compute_constant_lorebook_block(lorebook_entries, macros))),
        ("lorebook_depth", len(lorebook.depth_block)),
    ]
    if turn_cast.grouped:
        components[2:2] = [(key, len(text)) for key, text in group_components]
    breakdown = {label: {"chars": chars, "tokens_est": estimate_tokens(chars)} for label, chars in components}
    total_chars = sum(v["chars"] for v in breakdown.values())
    return {
        "total_chars": total_chars,
        "total_tokens_est": estimate_tokens(total_chars),
        "breakdown": breakdown,
        "message_count": len(messages),
        "estimate_kind": "maximum" if turn_cast.grouped else "single_call",
    }
