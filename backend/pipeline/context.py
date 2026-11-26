"""Load conversation context and prepare a pipeline turn."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .. import database as db
from ..core import (
    CardScripts,
    CastMember,
    ChatMessage,
    Macros,
    TurnCast,
    is_decision_row,
    parse_decision_definition,
)
from ..core.prompt_layout import LayoutBlock, default_layout
from ..core.settings import Settings
from ..database.models import (
    ActiveLorebookEntryRow,
    CharacterCardRow,
    ConversationRow,
    InteractiveFragmentRow,
    MoodFragmentRow,
    PhraseGroup,
    UserPersonaRow,
    WorldRow,
)
from ..features.lorebook import agentic_lorebook_active
from ..inference import (
    AbortToken,
    KVCacheTracker,
    LLMClient,
    agent_client_from_settings,
    client_from_settings,
    decisions_url,
    separate_agent_lane_configured,
)
from ..prompting import (
    build_prefix,
    char_context,
    conversation_macro_seed,
    conversation_sections,
    macro_identity,
    persona_macros,
    resolve_persona_id,
)
from ..prompting.layout import SectionInputs
from ..prompting.lorebook import (
    build_lorebook_catalog,
    compute_constant_lorebook_block,
    compute_depth_lorebook_block,
    compute_lorebook_injection_block,
)
from .config import build_writer_tools_blob, resolve_pipeline_config, split_interactive_fragments
from .events import PublicTurnEvent
from .passes.judge import DecisionCandidate, InvalidDecision, JudgeConfig, JudgePlan, judge_points
from .passes.state import StateContract
from .predicates import agent_enabled, world_proposal_active
from .state import BranchBaseline, LorebookTurn, PipelineConfig, WorldProposalTurn
from .workflow_bridge import iterate_pre_pipeline_hooks


@dataclass(frozen=True, slots=True)
class PipelineContext:
    """Per-conversation data loaded once for all entry points.

    Frozen field bindings still permit in-place director resets to branch state. ``agent_client`` is None without a separate
    endpoint. state_contract snapshots fragment config so concurrent settings edits cannot change the running turn.
    """

    settings: Settings
    conv: ConversationRow
    card: CharacterCardRow | None
    # Seeded from director_state, then carried as mutable per-turn director state (active moods, cooldowns, the branch's folded
    # state fragments); not all keys are columns.
    director: BranchBaseline
    mood_fragments: list[MoodFragmentRow]
    interactive_fragments: list[InteractiveFragmentRow]
    phrase_bank: list[PhraseGroup]
    lorebook_entries: list[ActiveLorebookEntryRow]
    client: LLMClient
    system_prompt: str
    # The conversation's macros, and the card and chat sections layout blocks name, resolved through them.
    macros: Macros
    sections: SectionInputs
    active_persona: UserPersonaRow | None
    agent_client: LLMClient | None
    # Every World row, unfiltered. Read by the Dynamic Worlds stage, which narrows it to its mutation targets through
    # ``world_proposal_active`` -- the enabled Worlds that opted in, i.e. the ones whose lore fed this turn.
    worlds: list[WorldRow] = field(default_factory=list)
    cast: TurnCast = field(default_factory=lambda: TurnCast(False, ()))
    speaker_names: Mapping[str, str] = field(default_factory=dict)
    speaker_scripts: Mapping[str, CardScripts] = field(default_factory=dict)
    card_scripts: CardScripts = field(default_factory=CardScripts)
    group_members: tuple[Mapping[str, Any], ...] = ()
    decision_candidates: tuple[DecisionCandidate, ...] = ()
    invalid_decisions: tuple[InvalidDecision, ...] = ()
    judge_config: JudgeConfig = field(default_factory=JudgeConfig)
    state_contract: StateContract = field(default_factory=StateContract)
    # Every defined fragment, enabled or not, for the shared tool schemas (see ``_defined_fragments``). None -- a context built
    # by hand -- offers exactly ``interactive_fragments``.
    defined_fragments: list[InteractiveFragmentRow] | None = None
    # The card each card row of ``defined_fragments`` comes from, by id; a global row has none.
    fragment_sources: Mapping[str, str] = field(default_factory=dict)
    # Where each enabled decision asks the Judge this turn.
    judge_plan: JudgePlan = field(default_factory=JudgePlan)
    # The active prompt layout, read once so every pass and speaker in the turn renders the same blocks.
    layout: tuple[LayoutBlock, ...] = field(default_factory=default_layout)


async def load_pipeline_context(conversation_id: str, *, abort_token: AbortToken | None = None) -> PipelineContext | None:
    """Load all per-conversation data needed by the pipeline.

    Fetches settings, conversation, card, director state, fragments, phrase bank, lorebook entries, and builds LLM clients. Both
    clients share the same *abort_token* so a single stop cancels every pass; a private token is created when none is supplied.

    Returns a :class:`PipelineContext`, or ``None`` if the conversation is missing.
    """
    abort_token = abort_token or AbortToken()
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director: BranchBaseline = {**await db.get_director_state(conversation_id)}
    card, active_persona = await resolve_card_and_persona(conv, settings)
    cast = await db.resolve_cast(conv)
    all_group_members = await db.get_group_members(conversation_id, include_inactive=True) if cast.grouped else []
    speaker_scripts = await db.get_group_member_scripts(conversation_id, members=all_group_members) if cast.grouped else {}
    # Card-embedded fragments merge into the global lists for this turn only
    # (the context is rebuilt per turn); on id collision the global wins.
    card_moods, card_interactive, card_fragment_sources = await db.cast_embedded_fragments(card, cast)
    mood_fragments = db.merge_fragments_by_id([f for f in await db.get_mood_fragments() if f.get("enabled", True)], card_moods)
    if director and director.get("active_moods"):
        director["active_moods"] = enabled_moods(director["active_moods"], mood_fragments)
    defined_fragments = _defined_fragments(await db.get_interactive_fragments(), card_interactive)
    # The enabled rows in blob order, so every pass lists its live fields in the order the shared schemas offer them. Card rows
    # are always enabled, so this is the enabled globals plus every card row whose id they do not claim.
    interactive_fragments = [row for row in defined_fragments if row.get("enabled", True)]
    decision_candidates, invalid_decisions = _decision_candidates(interactive_fragments, card_fragment_sources)
    card_rows = {id(row) for row in card_interactive}
    fragment_sources = {row["id"]: card_fragment_sources[row["id"]] for row in defined_fragments if id(row) in card_rows}
    phrase_bank = await db.get_phrase_bank()
    world_ids = await db.get_effective_world_ids(conversation_id)
    lorebook_entries = await db.get_active_lorebook_entries(world_ids)
    worlds = [world for world in await db.get_worlds() if world["id"] in world_ids]
    client = client_from_settings(settings, abort_token=abort_token)
    layout = await db.get_active_layout_blocks()
    card_text = char_context(settings, card, layout)
    macro_char, cast_names = macro_identity(conv, cast)
    macros, user_description = persona_macros(
        settings, macro_char, active_persona, seed=conversation_macro_seed(conv), card=card, cast=cast_names
    )
    agent_client = (
        agent_client_from_settings(settings, abort_token=abort_token) if separate_agent_lane_configured(settings) else None
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
        system_prompt=card_text.system_prompt,
        macros=macros,
        sections=conversation_sections(settings, conv, card_text, macros, user_description, cast),
        active_persona=active_persona,
        agent_client=agent_client,
        worlds=worlds,
        cast=cast,
        speaker_scripts=speaker_scripts,
        card_scripts=CardScripts.from_extensions(card.get("extensions") if card else None),
        speaker_names={m["id"]: m["display_name"] for m in all_group_members},
        group_members=tuple(m for m in all_group_members if m.get("active")),
        decision_candidates=decision_candidates,
        invalid_decisions=invalid_decisions,
        judge_config=await resolve_judge_config(settings),
        state_contract=StateContract.capture(settings, split_interactive_fragments(interactive_fragments)[2]),
        defined_fragments=defined_fragments,
        fragment_sources=fragment_sources,
        judge_plan=judge_points(interactive_fragments, fragment_sources),
        layout=layout,
    )


def enabled_moods(moods: Sequence[str], mood_fragments: Sequence[Mapping[str, Any]]) -> list[str]:
    """*moods* without the ids of disabled fragments, which no pass may see."""
    enabled_ids = {fragment["id"] for fragment in mood_fragments}
    return [mood for mood in moods if mood in enabled_ids]


def _defined_fragments(
    global_rows: Sequence[InteractiveFragmentRow], card_rows: Sequence[InteractiveFragmentRow]
) -> list[InteractiveFragmentRow]:
    """Every fragment the turn's tool schemas offer, in an order no toggle moves.

    Globals keep their sort order whether enabled or not, and cards follow, so enabling a fragment never reorders the blob. A
    disabled global yields its slot to the card row sharing its id -- the row the turn actually runs, since globals win only
    while enabled. The turn's enabled list is this list filtered, so every pass's live view keeps the blob's order.
    """
    card_by_id = {row["id"]: row for row in card_rows}
    rows = [row if row.get("enabled", True) or row["id"] not in card_by_id else card_by_id[row["id"]] for row in global_rows]
    return db.merge_fragments_by_id(rows, card_rows)


def _decision_candidates(
    fragments: Sequence[Mapping[str, Any]], card_fragment_sources: Mapping[str, str]
) -> tuple[tuple[DecisionCandidate, ...], tuple[InvalidDecision, ...]]:
    """Split decision rows into runnable definitions and rows to report as invalid."""
    candidates: list[DecisionCandidate] = []
    invalid: list[InvalidDecision] = []
    for row in fragments:
        if not is_decision_row(row):
            continue
        card_id = card_fragment_sources.get(row["id"])
        definition = parse_decision_definition(row)
        if definition is None:
            invalid.append(InvalidDecision(fragment_id=str(row["id"]), label=str(row.get("label") or ""), card_id=card_id))
            continue
        candidates.append(DecisionCandidate(definition=definition, card_id=card_id))
    return tuple(candidates), tuple(invalid)


async def resolve_judge_config(settings: Settings) -> JudgeConfig:
    """Resolve the Judge endpoint and derive its decisions route."""
    endpoint_id = settings.get("decision_endpoint_id")
    model = str(settings.get("decision_model") or "")
    if not endpoint_id or not model:
        return JudgeConfig()
    endpoint = await db.get_endpoint(int(endpoint_id))
    if endpoint is None:
        return JudgeConfig()
    return JudgeConfig(
        url=decisions_url(endpoint["url"]),
        api_key=endpoint.get("api_key", ""),
        model=model,
        proxy=endpoint.get("proxy", "") or "",
    )


async def resolve_card_and_persona(
    conv: Mapping[str, Any], settings: Settings
) -> tuple[CharacterCardRow | None, UserPersonaRow | None]:
    """Fetch the conversation's card and resolve the effective persona row.

    Applies the same conversation-pin -> card-pin -> global precedence as generation (:func:`resolve_persona_id`), so callers
    estimating or summarizing stay consistent with the prompt that is actually sent.
    """
    card_id = conv.get("character_card_id")
    card = await db.get_character_card(card_id) if card_id else None
    persona_id = resolve_persona_id(conv, card, settings)
    persona = await db.get_user_persona(persona_id) if persona_id else None
    return card, persona


def build_turn_prefix(
    ctx: PipelineContext,
    history: Sequence[Mapping[str, Any]],
    *,
    speaker: CastMember | None = None,
) -> list[ChatMessage]:
    """Build the prefix every lane of the turn shares.

    *speaker* applies to every pass, so the Editor's lane sees the same cast as the Writer it is auditing.
    """
    cast = ctx.cast._replace(speaker=speaker) if ctx.cast.grouped else ctx.cast
    return build_prefix(
        ctx.system_prompt,
        ctx.sections,
        history,
        ctx.macros,
        constant_lorebook_block=compute_constant_lorebook_block(ctx.lorebook_entries, ctx.macros),
        cast=cast,
        speaker_names=ctx.speaker_names,
        scripts=ctx.card_scripts,
        speaker_scripts=ctx.speaker_scripts,
        layout=ctx.layout,
    )


@dataclass(slots=True)
class TurnSetup:
    """Per-turn inputs produced by :func:`prepare_turn`, ready for ``run_pipeline``.

    Holds the shared prefix, the merged tool-enable map, macros, lorebook block, scratch dict, KV tracker, and dynamic-schema
    map.
    """

    prefix: list[ChatMessage]
    merged_enabled_tools: dict[str, bool]
    macros: Macros
    lorebook: LorebookTurn
    turn_scratch: dict
    kv_tracker: KVCacheTracker
    schema_overrides: Mapping[str, dict]
    # Identity of the Worlds this turn may propose changes to; None when no enabled World has opted in to Dynamic Worlds.
    world_proposal: WorldProposalTurn | None = None


def build_lorebook_turn(
    settings: Settings,
    entries: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    macros: Macros,
) -> LorebookTurn:
    """Decide how this turn's lorebook reaches the Director and the Writer."""
    # When agentic lorebook is active the keyword scan is skipped; the Director
    # picks entries from a catalog instead and the writer block is built post-director.
    agentic = agentic_lorebook_active(settings, entries, agent_on=agent_enabled(settings))
    return LorebookTurn(
        entries=entries,
        agentic=agentic,
        # Director-facing context: the agentic catalog, or the keyword-scanned block
        # (which the writer block reuses verbatim in substring mode).
        catalog=build_lorebook_catalog(entries) if agentic else "",
        block="" if agentic else compute_lorebook_injection_block(messages, entries, macros),
        # Rolled once here, so the writer and the editor replaying its content
        # agree on the dice (and a stopped/retried pass never re-rolls mid-turn).
        depth_block=compute_depth_lorebook_block(entries, macros),
    )


def _proposal_world_ids(ctx: PipelineContext, settings: Settings) -> tuple[str, ...]:
    # Resolved before the tools blob is built: enabling propose_world_changes is what emits its schema into the shared per-turn
    # blob, so the decision has to be made once, up front, and hold for every cached call in the turn.
    return tuple(str(w["id"]) for w in ctx.worlds if world_proposal_active(w, agent_on=agent_enabled(settings)))


def turn_tools(ctx: PipelineContext, settings: Settings) -> tuple[dict[str, bool], Mapping[str, dict]]:
    """The turn's tool map before pre-pipeline hooks extend it, and the schema overrides of its shared blob."""
    # When the agent is off, all tools are force-disabled.
    enabled_tools_setting = settings.get("enabled_tools") or {}
    if agent_enabled(settings):
        enabled_tools = dict(enabled_tools_setting)
    else:
        enabled_tools = {k: False for k in enabled_tools_setting}
    # Builds direct_scene plus the fragment-driven Editor and state tools from every defined fragment; must be called once so
    # all passes get byte-identical tool blobs (KV cache Invariants 3 & 5).
    defined = ctx.defined_fragments if ctx.defined_fragments is not None else ctx.interactive_fragments
    overrides, enabled_tools = build_writer_tools_blob(
        settings,
        defined,
        enabled_tools,
        # Offered on the setting, not on this turn's entries, so adding the first pickable entry leaves the blob alone.
        agentic_lorebook=agent_enabled(settings) and bool(settings.get("agentic_lorebook_enabled", 0)),
        dynamic_world=bool(_proposal_world_ids(ctx, settings)),
        grouped=ctx.cast.grouped,
        # Read over every defined row, so enabling or disabling a waiting decision never rewrites the blob.
        judged_between=bool(judge_points(defined, ctx.fragment_sources).waits_after),
    )
    return enabled_tools, MappingProxyType(overrides)


def offturn_config(ctx: PipelineContext, history: Sequence[Mapping[str, Any]]) -> PipelineConfig:
    """The config a turn after *history* would run with, built without its pre-pipeline hooks: the neutral prefix the Director
    reads, the shared tool blob, and the lanes, so an off-turn call extends that turn's cached prefix."""
    enabled_tools, schema_overrides = turn_tools(ctx, ctx.settings)
    return resolve_pipeline_config(
        ctx.settings,
        enabled_tools,
        macros=ctx.macros,
        client=ctx.client,
        agent_client=ctx.agent_client,
        prefix=build_turn_prefix(ctx, history),
        phrase_bank=ctx.phrase_bank,
        schema_overrides=schema_overrides,
    )


async def prepare_turn(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    settings: Settings,
    last_user_message: str,
    lorebook_messages: Sequence[Mapping[str, Any]],
) -> AsyncIterator[PublicTurnEvent | TurnSetup]:
    """Load and freeze per-turn context."""
    macros = ctx.macros
    macro_char = macros.char
    prefix_base = build_turn_prefix(ctx, history)

    turn_scratch: dict = {}
    kv_tracker = KVCacheTracker(conversation_id=conversation_id)
    lorebook = build_lorebook_turn(settings, ctx.lorebook_entries, lorebook_messages, macros)

    proposal_world_ids = _proposal_world_ids(ctx, settings)
    world_proposal = (
        WorldProposalTurn(
            world_ids=proposal_world_ids,
            conversation_id=conversation_id,
            user_message=last_user_message,
            character_label=macro_char if ctx.cast.grouped else ((ctx.card or {}).get("name", "") or macro_char),
            conversation_label=ctx.conv.get("title", "") or "",
        )
        if proposal_world_ids
        else None
    )

    enabled_tools_pre_merge, schema_overrides = turn_tools(ctx, settings)
    accumulators = {"merged_enabled_tools": dict(enabled_tools_pre_merge)}

    # Pre-pipeline hooks may extend the tool map.
    async for ev in iterate_pre_pipeline_hooks(
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

    yield TurnSetup(
        prefix=prefix_base,
        merged_enabled_tools=accumulators["merged_enabled_tools"],
        macros=macros,
        lorebook=lorebook,
        turn_scratch=turn_scratch,
        kv_tracker=kv_tracker,
        schema_overrides=schema_overrides,
        world_proposal=world_proposal,
    )
