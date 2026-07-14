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

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from .. import database as db
from ..core import ChatMessage, Macros
from ..database.models import (
    ActiveLorebookEntryRow,
    CharacterCardRow,
    ConversationRow,
    InteractiveFragmentRow,
    MoodFragmentRow,
    PhraseGroup,
    SettingsRow,
    UserPersonaRow,
)
from ..features.lorebook import (
    agentic_lorebook_active,
    build_lorebook_catalog,
    compute_lorebook_injection_block,
)
from ..inference import (
    AbortToken,
    LLMClient,
    _KVCacheTracker,
    agent_client_from_settings,
    build_prefix,
    client_from_settings,
)
from .config import _build_writer_tools_blob
from .predicates import agent_enabled, resolve_persona_id
from .state import LorebookTurn
from .workflow_bridge import _iterate_pre_pipeline_hooks


@dataclass(frozen=True)
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
    card: Optional[CharacterCardRow]
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
    active_persona: Optional[UserPersonaRow]
    agent_client: Optional[LLMClient]
    agent_system_prompt: Optional[str]


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
    mood_fragments = await db.get_mood_fragments()
    mood_fragments = [f for f in mood_fragments if f.get("enabled", True)]
    # Prune active moods that reference disabled fragments.
    if director and director.get("active_moods"):
        enabled_ids = {f["id"] for f in mood_fragments}
        director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
    interactive_fragments = await db.get_interactive_fragments()
    interactive_fragments = [df for df in interactive_fragments if df.get("enabled", True)]
    phrase_bank = await db.get_phrase_bank()
    lorebook_entries = await db.get_active_lorebook_entries()
    client = client_from_settings(settings, abort_token=abort_token)

    card, active_persona = await resolve_card_and_persona(conv, settings)
    system_prompt, char_persona, mes_example = await db.resolve_char_context(conv, settings, card=card)

    agent_same = settings.get("agent_same_as_writer", True)
    agent_client = None
    agent_system_prompt = None
    if not agent_same and settings.get("agent_endpoint_id"):
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


def persona_macros(settings: Mapping[str, Any], char_name: str, persona: Mapping[str, Any] | None) -> tuple[Macros, str]:
    """Build the turn :class:`Macros` plus the resolved user description.

    The description falls back to the global ``user_description`` setting when
    no persona row is active.
    """
    macros = Macros.from_settings(settings, char_name, persona)
    user_description = persona.get("description", "") if persona else settings.get("user_description", "")
    return macros, user_description


def _build_prefix_from_ctx(
    ctx: PipelineContext,
    history: Sequence[Mapping[str, Any]],
    *,
    system_prompt: str | None = None,
    extra_system_blocks: list[str] | None = None,
) -> list[ChatMessage]:
    """Build the LLM message prefix (system prompt + chat history) from *ctx*.

    *system_prompt* overrides ``ctx.system_prompt`` when given — used for the
    agent prefix in dual-model mode. *extra_system_blocks* are additional system
    sections contributed by pre-pipeline workflow hooks.
    """
    conv = ctx.conv
    macros, user_description = persona_macros(ctx.settings, conv["character_name"], ctx.active_persona)

    return build_prefix(
        system_prompt if system_prompt is not None else ctx.system_prompt,
        ctx.char_persona,
        conv["character_scenario"],
        ctx.mes_example,
        ("" if ctx.settings.get("prevent_prompt_overrides") else conv.get("post_history_instructions", "")),
        history,
        macros,
        user_description,
        extra_system_blocks=extra_system_blocks,
    )


def _build_prefixes(
    ctx: PipelineContext,
    history: Sequence[Mapping[str, Any]],
    *,
    extra_system_blocks: list[str] | None = None,
) -> tuple[list[ChatMessage], list[ChatMessage] | None]:
    """Build the writer prefix and optional agent prefix for a turn.

    Returns ``(prefix, agent_prefix)``. ``agent_prefix`` is ``None`` in
    single-model mode. *extra_system_blocks* from pre-pipeline hooks are applied
    to both so the system body stays identical across all passes.
    """
    prefix = _build_prefix_from_ctx(ctx, history, extra_system_blocks=extra_system_blocks)
    agent_sp = ctx.agent_system_prompt
    agent_prefix = (
        _build_prefix_from_ctx(
            ctx,
            history,
            system_prompt=agent_sp,
            extra_system_blocks=extra_system_blocks,
        )
        if agent_sp is not None
        else None
    )
    return prefix, agent_prefix


@dataclass
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
    macros = Macros.from_settings(ctx.settings, ctx.conv["character_name"], ctx.active_persona)

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
    )

    # Builds direct_scene + optionally give_feedback; must be called once so all
    # passes get byte-identical tool blobs (KV cache Invariants 3 & 5).
    overrides = _build_writer_tools_blob(
        settings, ctx.interactive_fragments, enabled_tools_pre_merge, agentic_lorebook=agentic_active
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
    )
