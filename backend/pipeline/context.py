"""
context.py — Everything that happens before the passes run.

Two phases:

* **Load** — :func:`_load_pipeline_context` fetches all per-conversation data
  (settings, conversation, card, director state, fragments, phrase bank,
  lorebook, LLM clients) into the frozen :class:`PipelineContext`, and
  :func:`_build_prefixes` builds the writer and optional agent message prefixes.
* **Prepare** — :func:`_prepare_turn` runs pre-pipeline workflow hooks (which may
  extend the tool map or system prompt), computes the lorebook block or agentic
  catalog, builds the tool blob, and yields a single :class:`_TurnSetup`.

LLM clients are built via :func:`backend.inference.client_from_settings` /
:func:`agent_client_from_settings` — tests substitute the streaming client by
patching ``backend.inference.client.LLMClient``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .. import database as db
from ..core import CastMember, ChatMessage, Macros, TurnCast
from ..database.models import (
    ActiveLorebookEntryRow,
    CharacterCardRow,
    ConversationRow,
    InteractiveFragmentRow,
    MoodFragmentRow,
    PhraseGroup,
    SettingsRow,
    UserPersonaRow,
    WorldRow,
)
from ..features.lorebook import (
    agentic_lorebook_active,
    build_lorebook_catalog,
    compute_constant_lorebook_block,
    compute_depth_lorebook_block,
    compute_lorebook_injection_block,
)
from ..inference import (
    AbortToken,
    LLMClient,
    _KVCacheTracker,
    agent_client_from_settings,
    build_prefix,
    client_from_settings,
    separate_agent_lane_configured,
)
from .config import _build_writer_tools_blob
from .predicates import agent_enabled, resolve_persona_id, world_proposal_active
from .state import LorebookTurn, WorldProposalTurn
from .workflow_bridge import _iterate_pre_pipeline_hooks


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Per-conversation data loaded once and threaded through every entry point.

    Frozen so field bindings are immutable. ``card`` and ``active_persona`` are
    None when absent. ``agent_client`` and ``agent_system_prompt`` are both None
    unless a separate agent endpoint is configured. ``director`` is a mutable
    dict deliberately mutated in place — the regenerate paths reset its
    ``active_moods`` and ``progressive_fields`` to the branch baseline, which the
    frozen dataclass allows (it guards rebinding, not mutating the pointed-at dict).
    """

    settings: SettingsRow
    conv: ConversationRow
    card: CharacterCardRow | None
    # Seeded from director_state, then carried as mutable per-turn director state
    # (active moods, progressive fields, direction notes); not all keys are columns.
    director: dict[str, Any]
    mood_fragments: list[MoodFragmentRow]
    interactive_fragments: list[InteractiveFragmentRow]
    phrase_bank: list[PhraseGroup]
    lorebook_entries: list[ActiveLorebookEntryRow]
    client: LLMClient
    system_prompt: str
    char_persona: str
    mes_example: str
    active_persona: UserPersonaRow | None
    agent_client: LLMClient | None
    agent_system_prompt: str | None
    # Every World row, unfiltered. Read by the Dynamic Worlds stage, which
    # narrows it to its mutation targets through ``world_proposal_active`` --
    # the enabled Worlds that opted in, i.e. the ones whose lore fed this turn.
    worlds: list[WorldRow] = field(default_factory=list)
    cast: TurnCast = field(default_factory=lambda: TurnCast(False, ()))
    speaker_names: Mapping[str, str] = field(default_factory=dict)
    group_members: tuple[Mapping[str, Any], ...] = ()


async def _load_pipeline_context(conversation_id: str, *, abort_token: AbortToken | None = None) -> PipelineContext | None:
    """Load all per-conversation data needed by the pipeline.

    Fetches settings, conversation, card, director state, fragments, phrase bank,
    lorebook entries, and builds LLM clients. Both clients share the same
    *abort_token* so a single stop cancels every pass; a private token is created
    when none is supplied.

    Returns a :class:`PipelineContext`, or ``None`` if the conversation is missing.
    """
    abort_token = abort_token or AbortToken()
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director: dict[str, Any] = dict(await db.get_director_state(conversation_id))
    card, active_persona = await resolve_card_and_persona(conv, settings)
    cast = await db.resolve_cast(conv)
    all_group_members = await db.get_group_members(conversation_id, include_inactive=True) if cast.grouped else []
    speaker_names = {m["id"]: m["display_name"] for m in all_group_members}
    # Card-embedded fragments merge into the global lists for this turn only
    # (the context is rebuilt per turn); on id collision the global wins.
    card_moods, card_interactive = db.card_embedded_fragments(card)
    if cast.grouped:
        seen_cards: set[str] = set()
        for member in cast.members:
            if not member.card_id or member.card_id in seen_cards:
                continue
            seen_cards.add(member.card_id)
            member_card = await db.get_character_card(member.card_id)
            moods, interactive = db.card_embedded_fragments(member_card)
            card_moods.extend(moods)
            card_interactive.extend(interactive)
    mood_fragments = await db.get_mood_fragments()
    mood_fragments = [f for f in mood_fragments if f.get("enabled", True)]
    mood_ids = {fragment["id"] for fragment in mood_fragments}
    for fragment in card_moods:
        if fragment["id"] not in mood_ids:
            mood_fragments.append(fragment)
            mood_ids.add(fragment["id"])
    # Prune active moods that reference disabled fragments.
    if director and director.get("active_moods"):
        enabled_ids = {f["id"] for f in mood_fragments}
        director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
    interactive_fragments = await db.get_interactive_fragments()
    interactive_fragments = [df for df in interactive_fragments if df.get("enabled", True)]
    interactive_ids = {fragment["id"] for fragment in interactive_fragments}
    for fragment in card_interactive:
        if fragment["id"] not in interactive_ids:
            interactive_fragments.append(fragment)
            interactive_ids.add(fragment["id"])
    phrase_bank = await db.get_phrase_bank()
    lorebook_entries = await db.get_active_lorebook_entries()
    worlds = await db.get_worlds()
    client = client_from_settings(settings, abort_token=abort_token)

    system_prompt, char_persona, mes_example = await db.resolve_char_context(conv, settings, card=card)

    agent_client = None
    agent_system_prompt = None
    if separate_agent_lane_configured(settings):
        agent_client = agent_client_from_settings(settings, abort_token=abort_token)
        agent_system_prompt, _, _ = await db.resolve_char_context(
            conv, settings, shared_key="agent_shared_system_prompt", card=card
        )

    return PipelineContext(
        settings=settings,
        conv=conv,
        card=card,
        director=director,
        mood_fragments=mood_fragments,
        interactive_fragments=interactive_fragments,
        phrase_bank=phrase_bank,
        lorebook_entries=lorebook_entries,
        client=client,
        system_prompt=system_prompt,
        char_persona=char_persona,
        mes_example=mes_example,
        active_persona=active_persona,
        agent_client=agent_client,
        agent_system_prompt=agent_system_prompt,
        worlds=worlds,
        cast=cast,
        speaker_names=speaker_names,
        group_members=tuple(m for m in all_group_members if m.get("active")),
    )


async def resolve_card_and_persona(
    conv: Mapping[str, Any], settings: Mapping[str, Any]
) -> tuple[CharacterCardRow | None, UserPersonaRow | None]:
    """Fetch the conversation's card and resolve the effective persona row.

    Applies the same conversation-pin → card-pin → global precedence as
    generation (:func:`resolve_persona_id`), so callers estimating or
    summarizing stay consistent with the prompt that is actually sent.
    """
    card_id = conv.get("character_card_id")
    card = await db.get_character_card(card_id) if card_id else None
    persona_id = resolve_persona_id(conv, card, settings)
    persona = await db.get_user_persona(persona_id) if persona_id else None
    return card, persona


def conversation_macro_seed(conv: Mapping[str, Any]) -> str:
    """The {{random}} seed for *conv*: its own id, unless a carried
    ``macro_seed`` (set by checkpoint/compress via ``fork_conversation``) pins
    picks to the source conversation so they match the copied history."""
    return conv.get("macro_seed") or conv["id"]


def persona_macros(
    settings: Mapping[str, Any], char_name: str, persona: Mapping[str, Any] | None, seed: str = ""
) -> tuple[Macros, str]:
    """Build the turn :class:`Macros` plus the resolved user description.

    The description falls back to the global ``user_description`` setting when
    no persona row is active. *seed* (:func:`conversation_macro_seed`) keeps
    {{random}} in per-turn-resolved prompt fields byte-stable per conversation.
    """
    macros = Macros.from_settings(settings, char_name, persona, seed=seed)
    user_description = persona.get("description", "") if persona else settings.get("user_description", "")
    return macros, user_description


def _build_prefix_from_ctx(
    ctx: PipelineContext,
    history: Sequence[Mapping[str, Any]],
    *,
    system_prompt: str | None = None,
    extra_system_blocks: list[str] | None = None,
    speaker: CastMember | None = None,
) -> list[ChatMessage]:
    """Build the LLM message prefix (system prompt + chat history) from *ctx*.

    *system_prompt* overrides ``ctx.system_prompt`` when given — used for the
    agent prefix in dual-model mode. *extra_system_blocks* are additional system
    sections contributed by pre-pipeline workflow hooks. Constant lorebook
    entries are rendered here into the system body (they are byte-identical
    every turn, so they belong in the cached prefix, not the trailing block) —
    except the ``at_depth`` ones, which ride ``LorebookTurn.depth_block``.

    *speaker* only reaches the system body under Classic card swap, which is
    the one mode whose prefix depends on who is speaking. Omitting it there
    yields the neutral base the Director and the pre-pipeline hooks run
    against — both execute before a speaking plan exists — and every speaker's
    base shares its bytes up to the active card.
    """
    conv = ctx.conv
    group_title = conv.get("title", "") if ctx.cast.grouped else conv["character_name"]
    macros, user_description = persona_macros(ctx.settings, group_title, ctx.active_persona, seed=conversation_macro_seed(conv))
    cast = ctx.cast
    if cast.grouped:
        macros = macros._replace(cast=", ".join(m.name for m in cast.members))
        cast = cast._replace(speaker=speaker)

    return build_prefix(
        system_prompt if system_prompt is not None else ctx.system_prompt,
        ctx.char_persona,
        conv["character_scenario"],
        ctx.mes_example,
        ("" if ctx.settings.get("prevent_prompt_overrides") else conv.get("post_history_instructions", "")),
        history,
        macros,
        user_description,
        constant_lorebook_block=compute_constant_lorebook_block(ctx.lorebook_entries, macros),
        extra_system_blocks=extra_system_blocks,
        cast=cast,
        speaker_names=ctx.speaker_names,
    )


def _build_prefixes(
    ctx: PipelineContext,
    history: Sequence[Mapping[str, Any]],
    *,
    extra_system_blocks: list[str] | None = None,
    speaker: CastMember | None = None,
) -> tuple[list[ChatMessage], list[ChatMessage] | None]:
    """Build the writer prefix and optional agent prefix for a turn.

    Returns ``(prefix, agent_prefix)``. ``agent_prefix`` is ``None`` in
    single-model mode. *extra_system_blocks* from pre-pipeline hooks are applied
    to both so the system body stays identical across all passes — and so is
    *speaker*, or the Editor's agent lane would see a different cast than the
    Writer it is auditing.
    """
    prefix = _build_prefix_from_ctx(ctx, history, extra_system_blocks=extra_system_blocks, speaker=speaker)
    agent_sp = ctx.agent_system_prompt
    agent_prefix = (
        _build_prefix_from_ctx(
            ctx,
            history,
            system_prompt=agent_sp,
            extra_system_blocks=extra_system_blocks,
            speaker=speaker,
        )
        if agent_sp is not None
        else None
    )
    return prefix, agent_prefix


@dataclass(slots=True)
class _TurnSetup:
    """Per-turn inputs produced by :func:`_prepare_turn`, ready for ``_run_pipeline``.

    Holds the (writer, agent) prefixes with any pre-pipeline system blocks
    already applied, the merged tool-enable map, macros, lorebook block, scratch
    dict, KV tracker, and dynamic-schema map.
    """

    prefix: list[ChatMessage]
    agent_prefix: list[ChatMessage] | None
    merged_enabled_tools: dict[str, bool]
    macros: Macros
    lorebook: LorebookTurn
    turn_scratch: dict
    kv_tracker: _KVCacheTracker
    schema_overrides: Mapping[str, dict]
    extra_system_blocks: tuple[str, ...]
    # Identity of the Worlds this turn may propose changes to; None when no
    # enabled World has opted in to Dynamic Worlds.
    world_proposal: WorldProposalTurn | None = None


async def _prepare_turn(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    last_user_message: str,
    lorebook_messages: Sequence[Mapping[str, Any]],
) -> AsyncIterator[dict | _TurnSetup]:
    """Prepare everything a turn needs before the pipeline starts.

    Builds macros, prefixes, tool maps, and the lorebook block; runs
    pre-pipeline workflow hooks (which may stream SSE events); then yields a
    single :class:`_TurnSetup` as the last item.

    Drain it as::

        setup = None
        async for ev in _prepare_turn(...):
            if isinstance(ev, _TurnSetup):
                setup = ev
            else:
                yield ev
        assert setup is not None
    """
    macro_char = ctx.conv.get("title", "") if ctx.cast.grouped else ctx.conv["character_name"]
    macros = Macros.from_settings(
        ctx.settings,
        macro_char,
        ctx.active_persona,
        seed=conversation_macro_seed(ctx.conv),
        cast=", ".join(m.name for m in ctx.cast.members) if ctx.cast.grouped else "",
    )

    prefix_base, agent_prefix_base = _build_prefixes(ctx, history)

    turn_scratch: dict = {}
    kv_tracker = _KVCacheTracker(conversation_id=conversation_id)
    # Built once; when the agent is off, all tools are force-disabled.
    enabled_tools_setting = settings.get("enabled_tools") or {}
    if agent_enabled(settings):
        enabled_tools_pre_merge = dict(enabled_tools_setting)
    else:
        enabled_tools_pre_merge = {k: False for k in enabled_tools_setting}

    # When agentic lorebook is active the keyword scan is skipped; the Director
    # picks entries from a catalog instead and the writer block is built post-director.
    agentic_active = agentic_lorebook_active(settings, ctx.lorebook_entries, agent_on=agent_enabled(settings))
    lorebook = LorebookTurn(
        entries=ctx.lorebook_entries,
        messages=lorebook_messages,
        agentic=agentic_active,
        # Director-facing context: the agentic catalog, or the keyword-scanned block
        # (which the writer block reuses verbatim in substring mode).
        catalog=build_lorebook_catalog(ctx.lorebook_entries) if agentic_active else "",
        block="" if agentic_active else compute_lorebook_injection_block(lorebook_messages, ctx.lorebook_entries, macros),
        # Rolled once here, so the writer and the editor replaying its content
        # agree on the dice (and a stopped/retried pass never re-rolls mid-turn).
        depth_block=compute_depth_lorebook_block(ctx.lorebook_entries, macros),
    )

    # Resolved before the tools blob is built: enabling propose_world_changes is
    # what emits its schema into the shared per-turn blob, so the decision has to
    # be made once, up front, and hold for every cached call in the turn.
    proposal_world_ids = tuple(str(w["id"]) for w in ctx.worlds if world_proposal_active(w, agent_on=agent_enabled(settings)))
    world_proposal = (
        WorldProposalTurn(
            world_ids=proposal_world_ids,
            conversation_id=conversation_id,
            user_message=last_user_message,
            character_label=(
                ctx.conv.get("title", "")
                if ctx.cast.grouped
                else ((ctx.card or {}).get("name", "") or ctx.conv.get("character_name", ""))
            ),
            conversation_label=ctx.conv.get("title", "") or "",
        )
        if proposal_world_ids
        else None
    )

    # Builds direct_scene + optionally give_feedback; must be called once so all
    # passes get byte-identical tool blobs (KV cache Invariants 3 & 5).
    overrides = _build_writer_tools_blob(
        settings,
        ctx.interactive_fragments,
        enabled_tools_pre_merge,
        agentic_lorebook=agentic_active,
        dynamic_world=world_proposal is not None,
        cast=(list(ctx.group_members) if ctx.cast.grouped else None),
    )
    schema_overrides = MappingProxyType(overrides)
    accumulators = {
        "merged_enabled_tools": dict(enabled_tools_pre_merge),
        "extras": [],
    }

    # Pre-pipeline hooks may extend the tool map or append system blocks.
    async for ev in _iterate_pre_pipeline_hooks(
        conversation_id=conversation_id,
        character_id=ctx.conv.get("character_card_id"),
        card=ctx.card,
        history=history,
        last_user_message=last_user_message,
        settings=settings,
        prefix_base=prefix_base,
        enabled_tools_pre_merge=enabled_tools_pre_merge,
        turn_scratch=turn_scratch,
        client=ctx.client,
        kv_tracker=kv_tracker,
        schema_overrides=schema_overrides,
        accumulators=accumulators,
    ):
        yield ev

    extras = accumulators["extras"]
    if extras:
        prefix, agent_prefix = _build_prefixes(ctx, history, extra_system_blocks=extras)
    else:
        prefix, agent_prefix = prefix_base, agent_prefix_base

    yield _TurnSetup(
        prefix=prefix,
        agent_prefix=agent_prefix,
        merged_enabled_tools=accumulators["merged_enabled_tools"],
        macros=macros,
        lorebook=lorebook,
        turn_scratch=turn_scratch,
        kv_tracker=kv_tracker,
        schema_overrides=schema_overrides,
        extra_system_blocks=tuple(extras),
        world_proposal=world_proposal,
    )
