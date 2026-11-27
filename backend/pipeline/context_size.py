"""Estimate the size of a conversation's next Writer call, broken down by prompt layout block."""

from __future__ import annotations

from typing import Any

from .. import database as db
from ..core import CardScripts, estimate_tokens, state_fragments_of
from ..database.models import ConversationRow
from ..prompting import (
    char_context,
    compute_style_injection_block,
    conversation_macro_seed,
    conversation_sections,
    largest_cast_section,
    macro_identity,
    persona_macros,
    render_history,
    render_state_body,
    resolve_mood_fragment_randoms,
)
from ..prompting.layout import PrefixInputs, WriterInputs, prefix_block_texts, writer_block_texts
from ..prompting.lorebook import compute_constant_lorebook_block
from .context import build_lorebook_turn, resolve_card_and_persona
from .passes.writer import speaker_frame
from .predicates import agent_enabled


def _row(row_id: str, label: str, kind: str, region: str, chars: int) -> dict[str, Any]:
    return {"id": row_id, "label": label, "kind": kind, "region": region, "chars": chars, "tokens_est": estimate_tokens(chars)}


async def estimate_context_size(conv: ConversationRow) -> dict[str, Any]:
    """Measure each block of the active prompt layout as a turn in *conv* would render it now, plus the chat history.

    Reads the same rows and applies the same merges as turn loading, but builds no model client, so a conversation without a
    configured endpoint still gets an estimate. A group's estimate is a *maximum* call, not a sum: the largest speaker's cast
    section and speaker frame.
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
    layout = await db.get_active_layout_blocks()
    card_text = char_context(settings, card, layout)
    # The same sections the prefix renders: a group's card-derived ones are empty, the cast section stands in.
    sections = conversation_sections(settings, conv, card_text, macros, user_desc, turn_cast)
    # Project the history through the same macros, card scripts, and group labels used by the model-facing prefix.
    history = render_history(
        messages,
        macros,
        cast=turn_cast,
        speaker_names=await db.get_speaker_names(cid) if turn_cast.grouped else None,
        scripts=CardScripts.from_extensions(card.get("extensions") if card else None),
        speaker_scripts=await db.get_group_member_scripts(cid) if turn_cast.grouped else None,
    )
    history_chars = 0
    for message in history:
        content = message["content"]
        if isinstance(content, str):
            history_chars += len(content)
        else:
            history_chars += sum(len(part["text"]) for part in content if part["type"] == "text")

    # Scene guidance -- fragment {{random}} resolves against a throwaway copy of the stored choice map so the estimate
    # matches the prompt bytes a real turn would inject, without recording new picks.
    active_moods = director.get("active_moods", [])
    est_mood_frags = resolve_mood_fragment_randoms(mood_frags, active_moods, dict(director.get("macro_choices", {})))
    direct_scene_enabled = agent_enabled(settings) and bool((settings.get("enabled_tools") or {}).get("direct_scene", False))
    scene_guidance = macros.at("scene_guidance").resolve_message(
        compute_style_injection_block(active_moods, active_moods, est_mood_frags, director_frags, direct_scene_enabled, {})
    )
    # The Writer's current-state block rides Scene guidance on every turn.
    current_state = ""
    writer_state = [fragment for fragment in state_fragments_of(director_frags) if fragment.injects_writer]
    if writer_state:
        state_block = render_state_body(writer_state, await db.fold_path_state(cid, [m["id"] for m in messages]))
        current_state = macros.at("state").resolve_message(state_block) if state_block else ""

    # The Writer's lorebook block as the turn builds it; with agentic lorebook on, the part known before the Director picks.
    lorebook = build_lorebook_turn(settings, lorebook_entries, messages, macros)

    # A group bills its largest speaker: the frame around the Writer request, and the macros the request's blocks resolve with.
    speaker_open, speaker_close, block_macros = "", "", macros
    if turn_cast.grouped:
        prevent = bool(settings.get("prevent_prompt_overrides"))
        frames = [
            speaker_frame(member, macros, turn_cast.context_mode, prevent_prompt_overrides=prevent)
            for member in turn_cast.members
            if not member.muted
        ]
        if frames:
            speaker_open, speaker_close, block_macros = max(frames, key=lambda frame: len(frame[0]) + len(frame[1]))

    prefix = PrefixInputs(
        main_prompt=card_text.system_prompt,
        cast=largest_cast_section(turn_cast, macros).lstrip("\n") if turn_cast.grouped else "",
        constant_lore=compute_constant_lorebook_block(lorebook_entries, macros),
        sections=sections,
    )
    writer = WriterInputs(
        selected_lore=lorebook.writer_block((), macros),
        scene_guidance=scene_guidance,
        current_state=current_state,
        depth_lore=lorebook.depth_block,
        sections=sections,
    )
    rows = [
        _row(block.id, block.label, block.kind, block.region, len(text))
        for block, text in prefix_block_texts(layout, prefix, macros)
    ]
    rows.append(_row("history", "Chat history", "history", "history", history_chars))
    if turn_cast.grouped:
        rows.append(_row("speaker", "Speaker", "speaker", "writer", len(speaker_open) + len(speaker_close)))
    rows += [
        _row(block.id, block.label, block.kind, block.region, len(text))
        for block, text in writer_block_texts(layout, writer, block_macros)
    ]
    total_chars = sum(row["chars"] for row in rows)
    return {
        "total_chars": total_chars,
        "total_tokens_est": estimate_tokens(total_chars),
        "breakdown": rows,
        "message_count": len(messages),
        "estimate_kind": "maximum" if turn_cast.grouped else "single_call",
    }
