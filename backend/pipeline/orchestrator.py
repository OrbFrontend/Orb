"""
orchestrator.py — Pipeline coordinator: director → writer → editor,
plus the public entry points handle_turn() and handle_regenerate().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Any, AsyncIterator, List, Mapping, Optional, Sequence

from .. import database as db
from ..core import (
    ChatMessage,
    Macros,
    extract_hyperparams,
    workflow_character_state_lock,
    workflow_state_lock,
)
from ..database.models import (
    ActiveLorebookEntryRow,
    CharacterCardRow,
    ConversationRow,
    DirectorStateRow,
    InteractiveFragmentRow,
    MoodFragmentRow,
    PhraseGroup,
    SettingsRow,
    UserPersonaRow,
)
from ..inference import (
    TOOLS,
    AbortToken,
    CachedBase,
    LLMClient,
    _KVCacheTracker,
    build_prefix,
    compute_lorebook_injection_block,
    enabled_schemas,
    reasoning_cfg,
)
from ..workflows import (
    HookType,
    PostCtx,
    PreCtx,
    _readonly,
    get_workflow,
    iter_subscriptions,
)
from ..workflows.attachment_cache import OVERSIZE_NO_METADATA_REASON
from .passes.director import (
    _agentic_lorebook_active,
    build_direct_scene_override,
    build_lorebook_catalog,
    director_stage,
)
from .passes.director.prompt_rewrite import disable_rewrite
from .passes.editor import _feedback_active, build_feedback_override, editor_stage
from .passes.editor.length_guard import (
    LengthGuard,
    apply_length_guard_tools,
    resolve_length_guard,
)
from .passes.writer import writer_stage
from .state import ModelLane, TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


# ── Core pipeline ─────────────────────────────────────────────────────────────


def is_dual_model(agent_client: LLMClient | None) -> bool:
    """Return True when a separate agent endpoint is configured (dual-model mode).

    Single-model: writer and agent share one endpoint and KV cache.
    Dual-model: agent (director + editor) runs on its own endpoint with a
    separate KV cache.
    """
    return agent_client is not None


def agent_enabled(settings: Mapping[str, Any]) -> bool:
    """Return True when the global Agent toggle is on (default).

    All agent-gated features — director, editor, length guard, feedback,
    mood/state persistence — read this single function so the default-on
    semantics stay consistent everywhere.
    """
    return bool(settings.get("enable_agent", 1))


def _resolve_pipeline_config(
    settings: Mapping[str, Any],
    enabled_tools: Mapping[str, bool],
    *,
    macros: Macros,
    client: LLMClient,
    agent_client: LLMClient | None,
    agent_prefix: list[ChatMessage] | None,
    prefix: list[ChatMessage],
    phrase_bank: list[PhraseGroup] | None,
    schema_overrides: Mapping[str, dict],
) -> _PipelineConfig:
    """Build the immutable per-turn config used throughout the pipeline.

    Resolves feature flags (audit, length guard, reasoning per pass), builds
    the two model lanes (writer and agent), and returns a :class:`_PipelineConfig`.
    Called once at the start of each turn by :func:`_run_pipeline` and
    :func:`handle_magic_rewrite`.
    """
    agent_on = agent_enabled(settings)
    reasoning_passes = settings.get("reasoning_enabled_passes") or {}

    audit_enabled = agent_on and bool(enabled_tools.get("editor_apply_patch", False)) and phrase_bank is not None

    # editor_rewrite is mirrored into the schema blob when the length guard is on.
    length_guard: LengthGuard | None = resolve_length_guard(settings, agent_on)
    enabled_tools = apply_length_guard_tools(enabled_tools, length_guard)

    # In dual-model mode the writer's KV cache is disjoint; skip tool schemas there.
    dual_model = is_dual_model(agent_client)
    writer_enabled_tools = {} if dual_model else enabled_tools

    writer_lane = ModelLane(
        client=client,
        base=CachedBase(
            prefix=tuple(prefix),
            tools=tuple(enabled_schemas(writer_enabled_tools, schema_overrides)),
            model=settings["model_name"],
            resolve=macros.resolve_prompt_messages,
        ),
    )
    if dual_model:
        assert agent_client is not None
        agent_lane = ModelLane(
            client=agent_client,
            base=CachedBase(
                prefix=tuple(agent_prefix or prefix),
                tools=tuple(enabled_schemas(enabled_tools, schema_overrides)),
                model=settings.get("agent_model_name", settings["model_name"]),
                resolve=macros.resolve_prompt_messages,
            ),
        )
    else:
        # Single-model: agent shares the writer's lane (same KV cache base).
        agent_lane = writer_lane

    return _PipelineConfig(
        agent_on=agent_on,
        enabled_tools=enabled_tools,
        director_reasoning_on=bool(reasoning_passes.get("director", True)),
        writer_reasoning_on=bool(reasoning_passes.get("writer", False)),
        editor_reasoning_on=bool(reasoning_passes.get("editor", False)),
        audit_enabled=audit_enabled,
        length_guard=length_guard,
        do_edit=audit_enabled or length_guard is not None,
        writer_enabled_tools=writer_enabled_tools,
        writer_lane=writer_lane,
        agent_lane=agent_lane,
    )


@dataclass
class _PipelineResult:
    """Terminal payload of the pipeline's internal ``_result`` event: the final
    draft plus everything the persistence path needs.

    It crosses the SSE hop as a plain dict (``as_event_data()``) so the
    ``_result`` event stays JSON-shaped for tests and inspectors, then is rebuilt
    into this typed form by :func:`_consume_pipeline` before the persist helpers
    read it. Every field defaults, so a turn aborted before ``_result`` ever
    fires (or a test injecting a partial payload) still produces a usable
    instance via the bare ``_PipelineResult()`` seed in :func:`_consume_pipeline`.
    """

    active_moods: list[str] = field(default_factory=list)
    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    rewritten_msg: str | None = None
    effective_msg: str = ""
    resp_text: str = ""
    inj_block: str = ""
    extra_fields: dict = field(default_factory=dict)
    progressive_fields: dict = field(default_factory=dict)
    reasoning_director: str = ""
    reasoning_writer: str = ""
    reasoning_editor: str = ""
    feedback: dict = field(default_factory=dict)
    staged_attachments: list[dict] = field(default_factory=list)
    staged_message_state: dict = field(default_factory=dict)

    def as_event_data(self) -> dict:
        """Shallow field dict for the ``_result`` SSE envelope. Shallow on
        purpose: ``staged_attachments`` carries raw artifact bytes that must not
        be deep-copied."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


def _make_result(state: TurnState, staged: list[dict] | None = None, staged_state: dict | None = None) -> dict:
    """Project the mutable :class:`TurnState` into the pipeline's terminal
    ``_result`` SSE event. *staged* / *staged_state* carry the post-pipeline
    workflow attachments and per-message state (empty on the writer-abort path,
    which fires before that iteration)."""
    return {
        "event": "_result",
        "data": _PipelineResult(
            active_moods=state.active_moods,
            agent_raw=state.agent_raw,
            calls=state.calls,
            latency=state.latency,
            rewritten_msg=state.rewritten_msg,
            effective_msg=state.effective_msg,
            resp_text=state.resp_text,
            inj_block=state.inj_block,
            extra_fields=state.extra_fields,
            progressive_fields=state.progressive_fields,
            reasoning_director=state.reasoning_director,
            reasoning_writer=state.reasoning_writer,
            reasoning_editor=state.reasoning_editor,
            feedback=state.feedback_values,
            staged_attachments=staged or [],
            staged_message_state=staged_state or {},
        ).as_event_data(),
    }


def _split_interactive_fragments(
    fragments: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split interactive fragments into writer vs. feedback groups.

    Returns ``(writer_fragments, feedback_fragments)``. ``field_type="feedback"``
    fragments are surfaced to the user via the post-writer feedback step and
    never reach the writer prompt; all others shape the ``direct_scene`` tool
    and the Scene Direction block.
    """
    writer = [df for df in fragments if df.get("field_type") != "feedback"]
    feedback = [df for df in fragments if df.get("field_type") == "feedback"]
    return writer, feedback


def _build_writer_tools_blob(
    settings: Mapping[str, Any],
    interactive_fragments: Sequence[Mapping[str, Any]],
    enabled_tools: dict,
    *,
    agentic_lorebook: bool = False,
) -> dict:
    """Build the dynamic tool schema overrides shared by every writer-cached call.

    Mutates *enabled_tools* in place to add ``give_feedback`` when feedback is
    active. Returns the ``schema_overrides`` dict (``direct_scene`` plus
    optionally ``give_feedback``) that keeps the tool blob byte-identical across
    the main turn and magic-rewrite so the LLM's KV cache is not busted.

    Called by :func:`_prepare_turn` and :func:`handle_magic_rewrite`.
    """
    writer_fragments, feedback_fragments = _split_interactive_fragments(interactive_fragments)
    overrides: dict = {"direct_scene": build_direct_scene_override(writer_fragments, agentic_lorebook=agentic_lorebook)}
    if _feedback_active(settings, feedback_fragments, agent_on=agent_enabled(settings)):
        overrides["give_feedback"] = build_feedback_override(feedback_fragments)
        enabled_tools["give_feedback"] = True
    return overrides


async def _run_pipeline(
    client: LLMClient,
    settings: Mapping[str, Any],
    director: Mapping[str, Any],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Optional[Sequence[Mapping[str, Any]]] = None,
    phrase_bank: list[PhraseGroup] | None = None,
    lorebook_block: str = "",
    lorebook_catalog: str = "",
    agentic_lorebook: bool = False,
    lorebook_entries: Sequence[Mapping[str, Any]] | None = None,
    editor_audit_msgs: list[str] | None = None,
    agent_client: LLMClient | None = None,
    agent_prefix: list[ChatMessage] | None = None,
    macros: Macros | None = None,
    conversation_id: str | None = None,
    character_id: str | None = None,
    card: Mapping[str, Any] | None = None,
    *,
    prefix: list[ChatMessage],
    enabled_tools: Mapping[str, bool],
    turn_scratch: dict,
    kv_tracker: _KVCacheTracker,
    schema_overrides: Mapping[str, dict],
    history: Sequence[Mapping[str, Any]] | None = None,
    lorebook_messages: Sequence[Mapping[str, Any]] | None = None,
) -> AsyncIterator[dict]:
    """Run the three-pass pipeline (director → writer → editor) for one turn.

    Streams SSE events as each pass runs, then drives the post-pipeline
    workflow hooks and emits a single ``_result`` event carrying the final
    draft and any workflow-staged attachments.

    If the user stops generation during the director pass the pipeline exits
    cleanly with no output. If they stop during the writer pass the partial
    draft is still carried out via ``_result`` so the caller can persist it.

    Called by :func:`_generate_reply` for every generating entry point.
    """
    if macros is None:
        macros = Macros("User", "")
    if attachments is None:
        attachments = []

    user_message = macros.resolve_message(user_message)

    # Resolved once; cfg.enabled_tools is the length-guard-folded map.
    cfg = _resolve_pipeline_config(
        settings,
        enabled_tools,
        macros=macros,
        client=client,
        agent_client=agent_client,
        agent_prefix=agent_prefix,
        prefix=prefix,
        phrase_bank=phrase_bank,
        schema_overrides=schema_overrides,
    )

    # feedback fragments are handled post-writer; the rest shape the writer prompt.
    writer_fragments, feedback_fragments = _split_interactive_fragments(interactive_fragments)

    # Mutable state threaded through the three passes; seeded from director + user message.
    _valid_progressive_ids = {df["id"] for df in writer_fragments if df.get("field_type") == "progressive"}
    state = TurnState(
        user_message=user_message,
        effective_msg=user_message,
        active_moods=director["active_moods"],
        progressive_state={k: v for k, v in director.get("progressive_fields", {}).items() if k in _valid_progressive_ids},
        valid_progressive_ids=_valid_progressive_ids,
    )

    # --- Director pass (+ rewrite, style injection, agentic-lorebook block) ---
    async for ev in director_stage(
        cfg,
        state,
        settings=settings,
        director=director,
        mood_fragments=mood_fragments,
        writer_fragments=writer_fragments,
        attachments=attachments,
        kv_tracker=kv_tracker,
        lorebook_block=lorebook_block,
        lorebook_catalog=lorebook_catalog,
        lorebook_entries=lorebook_entries,
        lorebook_messages=lorebook_messages,
        agentic_lorebook=agentic_lorebook,
        macros=macros,
    ):
        yield ev

    # Both clients share one abort token, so checking either is equivalent.
    if client.is_aborted:
        return

    # --- Writer pass ---
    async for ev in writer_stage(
        cfg,
        state,
        settings=settings,
        attachments=attachments,
        kv_tracker=kv_tracker,
    ):
        yield ev

    # Aborted mid-writer: persist partial output and skip remaining passes.
    if client.is_aborted:
        yield _make_result(state)
        kv_tracker.log_summary()
        return

    # --- Editor pass (edit loop + post-writer feedback step) ---
    async for ev in editor_stage(
        cfg,
        state,
        settings=settings,
        phrase_bank=phrase_bank,
        feedback_fragments=feedback_fragments,
        editor_audit_msgs=editor_audit_msgs,
        kv_tracker=kv_tracker,
    ):
        yield ev

    # --- Post-pipeline workflow iteration ---
    # director_output is a plain dict (PostCtx expects a read-only mapping).
    director_output = {
        "active_moods": state.active_moods,
        "agent_raw": state.agent_raw,
        "calls": state.calls,
        "latency": state.latency,
        "rewritten_msg": state.rewritten_msg,
        "extra_fields": state.extra_fields,
        "progressive_fields": state.progressive_fields,
    }
    post: _PostPipelineResult | None = None
    async for ev in _run_post_pipeline(
        draft=state.resp_text,
        conversation_id=conversation_id,
        character_id=character_id,
        card=card,
        history=history,
        effective_msg=state.effective_msg,
        director_output=director_output,
        settings=settings,
        prefix=prefix,
        enabled_tools=cfg.enabled_tools,
        turn_scratch=turn_scratch,
        client=client,
        kv_tracker=kv_tracker,
        schema_overrides=schema_overrides,
    ):
        if isinstance(ev, _PostPipelineResult):
            post = ev
        else:
            yield ev
    assert post is not None

    # Fold any hook-rewritten draft back into state before emitting _result.
    state.resp_text = post.draft
    yield _make_result(state, post.staged_attachments, post.staged_message_state)
    kv_tracker.log_summary()


@dataclass
class _PostPipelineResult:
    """Terminal value of :func:`_run_post_pipeline`: the (possibly hook-rewritten)
    draft and the attachments / per-message state staged for persistence."""

    draft: str
    staged_attachments: list[dict]
    staged_message_state: dict[str, dict]


async def _run_post_pipeline(
    *,
    draft: str,
    conversation_id: str | None,
    character_id: str | None,
    card: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]] | None,
    effective_msg: str,
    director_output: dict,
    settings: Mapping[str, Any],
    prefix: list[ChatMessage],
    enabled_tools: Mapping[str, bool],
    turn_scratch: dict,
    client: LLMClient,
    kv_tracker: _KVCacheTracker,
    schema_overrides: Mapping[str, dict],
) -> AsyncIterator[dict | _PostPipelineResult]:
    """Run every POST_PIPELINE workflow hook over the finished draft.

    Streams pass-through SSE events from each hook and yields one terminal
    :class:`_PostPipelineResult` when all hooks have run. Each hook may
    replace the draft once, attach artifacts, or set per-message state.
    Hook failures are logged and skipped so one bad hook cannot crash a turn.

    Called by :func:`_run_pipeline` after the editor pass.
    """
    staged_attachments: list[dict] = []
    staged_message_state: dict[str, dict] = {}
    for sub in iter_subscriptions(HookType.POST_PIPELINE):
        replaced_this_hook = False
        # Serialize same-(cid, workflow_id) writers against concurrent
        # /trigger calls and any other in-flight pipeline that reaches this
        # hook on the same conversation. Different workflows on the same
        # conversation keep distinct lock keys, so they still run in parallel.
        # Serialize same-(cid, wid) writers; different workflows run in parallel.
        async with (
            workflow_state_lock(conversation_id or "", sub.workflow_id),
            workflow_character_state_lock(character_id or "", sub.workflow_id),
        ):
            try:
                post_ctx = PostCtx(
                    conversation_id=conversation_id or "",
                    history=_readonly(history or []),
                    draft=draft,
                    effective_msg=effective_msg,
                    director_output=_readonly(director_output),
                    settings=_readonly(settings),
                    prefix=_readonly(prefix),
                    enabled_tools=_readonly(enabled_tools),
                    turn_scratch=turn_scratch,
                    client=client,
                    kv_tracker=kv_tracker,
                    schema_overrides=_readonly(schema_overrides),
                    character_id=character_id,
                    character=_readonly(card),
                )
                async for ev in sub.callable(post_ctx):
                    t = ev.get("type") if isinstance(ev, dict) else None
                    if t == "draft_replaced":
                        if replaced_this_hook:
                            logger.warning(
                                "post_pipeline hook %r yielded a second draft_replaced; ignoring",
                                sub.workflow_id,
                            )
                            continue
                        new_draft = ev.get("draft")
                        if not isinstance(new_draft, str) or new_draft == draft:
                            logger.warning(
                                "post_pipeline hook %r yielded malformed draft_replaced "
                                "(draft type=%s, unchanged=%s); ignoring",
                                sub.workflow_id,
                                type(new_draft).__name__,
                                new_draft == draft,
                            )
                            continue
                        draft = new_draft
                        replaced_this_hook = True
                        yield {
                            "event": "writer_rewrite",
                            "data": {"refined_text": draft},
                        }
                        continue
                    if t == "attach_artifact":
                        # Only workflows with produces_artifacts=True may persist attachments.
                        w = get_workflow(sub.workflow_id)
                        if not (w and w.produces_artifacts):
                            logger.warning(
                                "post_pipeline hook %r yielded attach_artifact but "
                                "workflow does not declare produces_artifacts=True; "
                                "dropping entry",
                                sub.workflow_id,
                            )
                            continue
                        staged = _stage_workflow_attachment(
                            ev.get("attachment") if isinstance(ev, dict) else None,
                            sub.workflow_id,
                        )
                        if staged is not None:
                            staged_attachments.append(staged)
                        continue
                    if t == "set_message_state":
                        # Written in _persist_result once the assistant row id is known.
                        state = ev.get("state") if isinstance(ev, dict) else None
                        if not isinstance(state, dict):
                            logger.warning(
                                "post_pipeline hook %r yielded set_message_state with non-dict state (type=%s); ignoring",
                                sub.workflow_id,
                                type(state).__name__,
                            )
                            continue
                        staged_message_state[sub.workflow_id] = state
                        continue
                    # Reject reserved internal events (underscore-prefixed) so hooks
                    # cannot impersonate _result and trigger spurious persistence.
                    e_name = ev.get("event") if isinstance(ev, dict) else None
                    if isinstance(e_name, str) and e_name.startswith("_"):
                        logger.warning(
                            "post_pipeline hook %r yielded reserved internal event %r; dropping",
                            sub.workflow_id,
                            e_name,
                        )
                        continue
                    yield ev
            except Exception:
                logger.exception("post_pipeline hook %r failed", sub.workflow_id)

    yield _PostPipelineResult(draft, staged_attachments, staged_message_state)


def _stage_workflow_attachment(att: object, workflow_id: str) -> dict | None:
    """Validate and normalize a workflow ``attach_artifact`` entry.

    Returns a bytes-only dict ready for ``add_message``, or ``None`` if
    validation fails (with a logged warning). Never raises — bad workflow
    output must not crash a turn.

    Called by :func:`_run_post_pipeline` for each ``attach_artifact`` yield.
    """
    if not isinstance(att, dict):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with non-dict attachment (type=%s); ignoring",
            workflow_id,
            type(att).__name__,
        )
        return None

    expected_source = f"workflow:{workflow_id}"
    filename = att.get("filename")
    mime = att.get("mime")
    has_data = "data" in att
    has_path = "path" in att
    annotation_present = "annotation" in att
    raw_annotation = att.get("annotation")

    valid = (
        isinstance(filename, str)
        and isinstance(mime, str)
        and (has_data != has_path)
        and ((not has_data) or isinstance(att["data"], (bytes, bytearray)))
        and ((not has_path) or isinstance(att["path"], str))
        and ((not annotation_present) or raw_annotation is None or isinstance(raw_annotation, str))
        and att.get("source") == expected_source
        and att.get("workflow_id") == workflow_id
    )
    if not valid:
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact failing validation "
            "(filename/mime/data-xor-path/source/workflow_id/annotation); ignoring entry",
            workflow_id,
        )
        return None

    out = dict(att)
    # Whitespace-only annotation collapses to None ("no LLM-visible footprint").
    if isinstance(raw_annotation, str) and not raw_annotation.strip():
        out["annotation"] = None

    raw_cm = out.get("consumption_metadata")
    if raw_cm is not None and not isinstance(raw_cm, dict):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with non-dict consumption_metadata "
            "(filename=%r, type=%s); coercing to None",
            workflow_id,
            filename,
            type(raw_cm).__name__,
        )
        out["consumption_metadata"] = None

    if has_path:
        try:
            with open(att["path"], "rb") as f:
                data_bytes = f.read()
        except OSError as e:
            logger.warning(
                "post_pipeline hook %r yielded attach_artifact with path=%r that failed to read (%s); dropping entry",
                workflow_id,
                att["path"],
                e,
            )
            return None
        out.pop("path", None)
        out["data"] = data_bytes
    else:
        out["data"] = bytes(att["data"])

    if not out.get("data"):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with empty data (filename=%r); dropping entry",
            workflow_id,
            filename,
        )
        return None

    return out


async def _iterate_pre_pipeline_hooks(
    *,
    conversation_id: str,
    character_id: str | None = None,
    card: Mapping[str, Any] | None = None,
    history: Sequence[Mapping[str, Any]],
    last_user_message: str,
    settings: Mapping[str, Any],
    prefix_base: list[ChatMessage],
    enabled_tools_pre_merge: Mapping[str, bool],
    turn_scratch: dict,
    client,
    kv_tracker,
    schema_overrides: Mapping[str, dict],
    accumulators: dict,
) -> AsyncIterator[dict]:
    """Run every PRE_PIPELINE workflow hook before the main pipeline starts.

    Yields pass-through SSE events and mutates *accumulators* in place:
    ``enable_tools`` yields fold extra tools into the merged map;
    ``system_prompt`` yields append blocks to the extras list. Hook failures
    are logged and skipped.

    *accumulators* must be pre-populated with
    ``{"merged_enabled_tools": <dict>, "extras": []}``.

    Called by :func:`_prepare_turn`.
    """
    for sub in iter_subscriptions(HookType.PRE_PIPELINE):
        # Lock held for the hook's full lifetime to keep workflow_state RMW atomic.
        async with (
            workflow_state_lock(conversation_id, sub.workflow_id),
            workflow_character_state_lock(character_id or "", sub.workflow_id),
        ):
            try:
                pre_ctx = PreCtx(
                    conversation_id=conversation_id,
                    history=_readonly(history),
                    last_user_message=last_user_message,
                    settings=_readonly(settings),
                    prefix=_readonly(prefix_base),
                    enabled_tools_pre_merge=_readonly(enabled_tools_pre_merge),
                    turn_scratch=turn_scratch,
                    client=client,
                    kv_tracker=kv_tracker,
                    schema_overrides=_readonly(schema_overrides),
                    character_id=character_id,
                    character=_readonly(card),
                )
                async for ev in sub.callable(pre_ctx):
                    t = ev.get("type") if isinstance(ev, dict) else None
                    if t == "enable_tools":
                        tools = ev.get("tools")
                        if isinstance(tools, (set, frozenset)):
                            items = ((n, True) for n in tools)
                        elif isinstance(tools, dict):
                            items = tools.items()
                        else:
                            logger.warning(
                                "pre_pipeline hook %r yielded enable_tools with invalid tools payload (type=%s); ignoring",
                                sub.workflow_id,
                                type(tools).__name__,
                            )
                            continue
                        for name, val in items:
                            if val is not True:
                                logger.warning(
                                    "workflow %r yielded enable_tools %r=%r; only True is honored, entry dropped",
                                    sub.workflow_id,
                                    name,
                                    val,
                                )
                                continue
                            if name not in TOOLS:
                                logger.warning(
                                    "workflow %r enabled unregistered tool %r; dropping",
                                    sub.workflow_id,
                                    name,
                                )
                                continue
                            accumulators["merged_enabled_tools"][name] = True
                        continue
                    if t == "system_prompt":
                        block = ev.get("block")
                        if not isinstance(block, str) or not block.strip():
                            logger.warning(
                                "pre_pipeline hook %r yielded empty/whitespace-only system_prompt; ignoring",
                                sub.workflow_id,
                            )
                            continue
                        accumulators["extras"].append(block)
                        continue
                    # Reject reserved internal events (defense-in-depth).
                    e_name = ev.get("event") if isinstance(ev, dict) else None
                    if isinstance(e_name, str) and e_name.startswith("_"):
                        logger.warning(
                            "pre_pipeline hook %r yielded reserved internal event %r; dropping",
                            sub.workflow_id,
                            e_name,
                        )
                        continue
                    yield ev
            except Exception:
                logger.exception("pre_pipeline hook %r failed", sub.workflow_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared infrastructure for handle_turn / handle_regenerate
# ═══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PipelineContext:
    """Resolved per-conversation inputs the pipeline needs, loaded once by
    :func:`_load_pipeline_context` and threaded through every entry point.

    Frozen so the *binding* of each field is immutable; the optional fields make
    explicit what was previously only implied by the ``ctx[...]`` vs
    ``ctx.get(...)`` split in readers. ``card`` / ``active_persona`` are None when
    the conversation has no character card / no active user persona; ``agent_client``
    and ``agent_system_prompt`` are None unless a separate agent endpoint is
    configured (they travel together). ``director`` is a mutable dict held by
    reference and deliberately mutated in place — the regenerate paths reset its
    ``active_moods`` / ``progressive_fields`` to the branch baseline before a turn,
    which the frozen dataclass does not prevent (it guards rebinding the field,
    not mutating the dict it points at).
    """

    settings: SettingsRow
    conv: ConversationRow
    card: Optional[CharacterCardRow]
    director: DirectorStateRow
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


def resolve_persona_id(
    conv: Mapping[str, Any],
    card: Mapping[str, Any] | None,
    settings: Mapping[str, Any],
) -> int | None:
    """Resolve the effective persona id for a turn.

    A locked persona overrides the global active persona within its scope.
    Priority: conversation lock → character-card lock → global active persona.
    """
    return conv.get("persona_lock_id") or (card.get("persona_lock_id") if card else None) or settings.get("active_persona_id")


async def _load_pipeline_context(conversation_id: str, *, abort_token: AbortToken | None = None) -> PipelineContext | None:
    """Load all per-conversation inputs needed to run the pipeline.

    Fetches settings, conversation, character card, director state, fragments,
    phrase bank, lorebook entries, and builds LLM clients. Both the writer and
    agent clients share the same *abort_token* so a single ``/stop`` cancels
    every pass. Creates a private token when none is supplied.

    Returns a :class:`PipelineContext`, or ``None`` if the conversation was not found.

    Called by every public entry point before any pipeline work begins.
    """
    abort_token = abort_token or AbortToken()
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director = await db.get_director_state(conversation_id)
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
    client = LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        abort_token=abort_token,
    )

    card_id = conv.get("character_card_id")
    card = await db.get_character_card(card_id) if card_id else None
    system_prompt, char_persona, mes_example = await db.resolve_char_context(conv, settings, card=card)

    active_persona = None
    active_persona_id = resolve_persona_id(conv, card, settings)
    if active_persona_id:
        active_persona = await db.get_user_persona(active_persona_id)

    agent_same = settings.get("agent_same_as_writer", True)
    agent_client = None
    agent_system_prompt = None
    if not agent_same and settings.get("agent_endpoint_id"):
        agent_url = settings.get("agent_endpoint_url", settings["endpoint_url"])
        agent_api_key = settings.get("agent_api_key", settings.get("api_key", ""))
        agent_client = LLMClient(
            agent_url,
            api_key=agent_api_key,
            abort_token=abort_token,
        )
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


def _build_prefix_from_ctx(
    ctx: PipelineContext,
    history: Sequence[Mapping[str, Any]],
    *,
    system_prompt: str | None = None,
    extra_system_blocks: list[str] | None = None,
) -> list[ChatMessage]:
    """Build the LLM message prefix (system prompt + chat history) from *ctx*.

    *system_prompt* overrides ``ctx.system_prompt`` when provided — used for
    the agent prefix in dual-model mode. *extra_system_blocks* appends
    additional system sections contributed by pre-pipeline hooks.

    Called by :func:`_build_prefixes`.
    """
    conv = ctx.conv
    active_persona = ctx.active_persona
    macros = Macros.from_settings(ctx.settings, conv["character_name"], active_persona)
    user_description = active_persona.get("description", "") if active_persona else ctx.settings.get("user_description", "")

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

    Returns ``(prefix, agent_prefix)``. ``agent_prefix`` is ``None`` when no
    separate agent system prompt is configured (single-model mode).
    *extra_system_blocks* from pre-pipeline hooks is applied to both so the
    system body stays identical across all passes.

    Called by :func:`_prepare_turn`.
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


def _compute_lorebook(macros: Macros, ctx: PipelineContext, messages: Sequence[Mapping[str, Any]]) -> str:
    """Scan *messages* for lorebook keyword matches and return the injection block.

    Called by :func:`_prepare_turn` when agentic lorebook mode is off.
    """
    return compute_lorebook_injection_block(
        messages,
        ctx.lorebook_entries,
        macros,
    )


@dataclass
class _TurnSetup:
    """Resolved per-turn pipeline inputs produced by :func:`_prepare_turn`.

    Bundles everything the entry points compute identically between persisting
    the user row and launching ``_run_pipeline``: the (writer, agent) prefixes
    with any pre-pipeline ``system_prompt`` blocks already applied, the merged
    tool-enable map, and the per-turn shared identities (macros, lorebook
    block, scratch dict, KV tracker, dynamic-schema map).
    """

    prefix: list[ChatMessage]
    agent_prefix: list[ChatMessage] | None
    merged_enabled_tools: dict[str, bool]
    macros: Macros
    lorebook_block: str
    turn_scratch: dict
    kv_tracker: _KVCacheTracker
    schema_overrides: Mapping[str, dict]
    # Agentic-lorebook activation: when active, ``lorebook_block`` is "" (the
    # keyword scan is bypassed), ``lorebook_catalog`` carries the Director's
    # candidate catalog, and the writer block is computed post-director from the
    # selection. When inactive both are inert (catalog "", flag False).
    lorebook_catalog: str = ""
    agentic_lorebook_active: bool = False


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

    Builds macros, prefixes, tool maps, the lorebook block, runs pre-pipeline
    workflow hooks (which may stream SSE events), then yields a single
    :class:`_TurnSetup` as the last item.

    Drain it as::

        setup = None
        async for ev in _prepare_turn(...):
            if isinstance(ev, _TurnSetup):
                setup = ev
            else:
                yield ev
        assert setup is not None

    Called by :func:`_generate_reply` for every generating entry point.
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
    agentic_active = _agentic_lorebook_active(
        settings, enabled_tools_pre_merge, ctx.lorebook_entries, agent_on=agent_enabled(settings)
    )
    if agentic_active:
        lorebook_block = ""
        lorebook_catalog = build_lorebook_catalog(ctx.lorebook_entries)
    else:
        lorebook_block = _compute_lorebook(macros, ctx, lorebook_messages)
        lorebook_catalog = ""

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
        lorebook_block=lorebook_block,
        turn_scratch=turn_scratch,
        kv_tracker=kv_tracker,
        schema_overrides=schema_overrides,
        lorebook_catalog=lorebook_catalog,
        agentic_lorebook_active=agentic_active,
    )


def _conversation_log_writer(conversation_id: str, log_turn_index: int):
    """Return an async callback that writes the turn's ``conversation_logs`` row.

    The callback is passed as ``extra_on_result`` to :func:`_consume_pipeline`
    and runs right after the assistant message is persisted. Fresh turns log at
    the user turn index; branch-creating paths (fork-edit, regenerate) log at
    the assistant turn index so branches stay distinguishable in the log.
    """

    async def _on_result(res: _PipelineResult, asst_id):
        await db.add_conversation_log(
            conversation_id,
            log_turn_index,
            res.agent_raw,
            res.calls,
            res.active_moods,
            res.inj_block,
            res.latency,
            res.progressive_fields,
            message_id=asst_id,
            reasoning_director=res.reasoning_director,
            reasoning_writer=res.reasoning_writer,
            reasoning_editor=res.reasoning_editor,
            feedback=res.feedback,
        )

    return _on_result


async def _resolve_target_and_parent(
    conversation_id: str, assistant_msg_id: int
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | str:
    """Load an assistant message and its parent user message.

    Returns ``(target, user_msg)`` on success, or an error string if the
    message is missing, belongs to a different conversation, or is not an
    assistant message.

    Called by all regenerate-style entry points.
    """
    target = await db.get_message_by_id(assistant_msg_id)
    if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
        return "Invalid target message"
    user_msg_id = target["parent_id"]
    user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
    if not user_msg:
        return "Parent user message not found"
    return target, user_msg


async def _prepare_regen_context(
    ctx: PipelineContext,
    conversation_id: str,
    target: Mapping[str, Any],
    user_msg: Mapping[str, Any],
) -> tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]:
    """Load history and attachments for a regeneration pass.

    Also resets the director's active moods and progressive fields to the
    pre-turn baseline so the regenerated reply starts from the same state
    as the original. Returns ``(history, attachments)``.

    Called by :func:`handle_regenerate` and :func:`handle_super_regenerate`.
    """
    parent_id: int | None = user_msg.get("parent_id")
    history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []
    moods_before = await db.get_moods_before_turn(conversation_id, target["turn_index"] - 1)
    ctx.director["active_moods"] = moods_before
    grandparent = next((m for m in reversed(history) if m["role"] == "assistant"), None)
    ctx.director["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}
    user_msg_id = target["parent_id"]
    attachments = await db.get_user_attachments_for_message(user_msg_id) if user_msg_id else []
    return history, attachments


async def _persist_rewrite(res: _PipelineResult, user_msg_id: int | None) -> None:
    """Overwrite the stored user message with the director's rewrite, if any.

    No-op when the director did not rewrite the message. Shared by both the
    normal and fallback persistence paths so the logic lives in one place.
    """
    if res.rewritten_msg and user_msg_id:
        await db.update_message_content(user_msg_id, res.effective_msg)


async def _persist_result(
    conversation_id: str,
    res: _PipelineResult,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
) -> tuple[int | None, list[dict]]:
    """Persist the assistant message and all turn side-effects after ``_result`` fires.

    Updates director state, saves the assistant message row with any workflow
    attachments, writes per-message workflow state, advances the active leaf,
    and increments the lifetime character counter.

    Returns ``(asst_id, rejected_workflow_atts)``. ``rejected_workflow_atts``
    is non-empty when the attachment cache dropped entries that lacked the
    metadata needed for re-synthesis.

    Called by :func:`_consume_pipeline`.
    """
    if agent_enabled(settings):
        await db.update_director_state(
            conversation_id,
            res.active_moods,
            progressive_fields=res.progressive_fields,
        )
    await _persist_rewrite(res, user_msg_id)

    # Skip persistence if the LLM produced no content tokens (e.g. reasoning-only).
    resp_text = res.resp_text
    if resp_text.strip():
        # Attachments ride the same INSERT transaction; aborted turns leave no orphans.
        staged = res.staged_attachments or None
        asst_id, rejected = await db.add_message(
            conversation_id,
            "assistant",
            resp_text,
            turn_index,
            parent_id=user_msg_id,
            attachments=staged,
            progressive_fields=res.progressive_fields,
        )
        # Row id only known here; no other caller can name it yet, so no lock needed.
        for wid, payload in res.staged_message_state.items():
            try:
                await db.set_workflow_message_state(asst_id, wid, payload)
            except Exception:
                logger.exception(
                    "Failed to persist workflow message state (wid=%r) for assistant message %s; "
                    "row already committed, continuing",
                    wid,
                    asst_id,
                )
        try:
            await db.set_active_leaf(conversation_id, asst_id)
        except Exception:
            logger.exception(
                "Failed to set active leaf to assistant message %s; row already committed",
                asst_id,
            )
        # Counter seed scans existing rows, so this must run after add_message.
        try:
            await db.add_generated_chars(len(resp_text))
        except Exception:
            logger.exception("Failed to update generated-chars counter; row already committed")
        return asst_id, rejected
    else:
        logger.info("Skipping assistant message persistence: resp_text is empty (reasoning‑only output)")
        return None, []


async def _fallback_persist(
    conversation_id: str,
    res: _PipelineResult,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
):
    """Best-effort save for a turn that was aborted before ``_result`` fired.

    Saves whatever the writer streamed (``accumulated_text``) if non-empty.
    Reasoning-only output does not create a message node. Errors are swallowed
    so a save failure does not propagate to the caller.

    Called from the ``finally`` block of :func:`_consume_pipeline`.
    """
    try:
        if res.active_moods and agent_enabled(settings):
            await db.update_director_state(
                conversation_id,
                res.active_moods,
                progressive_fields=res.progressive_fields,
            )
        await _persist_rewrite(res, user_msg_id)

        # accumulated_text holds only writer tokens (not reasoning deltas).
        if accumulated_text.strip():
            asst_id, _ = await db.add_message(
                conversation_id,
                "assistant",
                accumulated_text,
                turn_index,
                parent_id=user_msg_id,
            )
            await db.set_active_leaf(conversation_id, asst_id)
            logger.info(
                "Fallback persistence saved incomplete assistant message (%d chars)",
                len(accumulated_text),
            )
    except Exception:
        logger.exception("Fallback persistence failed")


async def _shielded_fallback(
    conversation_id: str,
    res: _PipelineResult,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
):
    """Run :func:`_fallback_persist` under ``asyncio.shield``, retrying once on cancellation.

    Ensures partial output is saved even when the surrounding request task is
    cancelled mid-write. Called from the ``finally`` block of :func:`_consume_pipeline`.
    """
    try:
        await asyncio.shield(
            _fallback_persist(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
            )
        )
    except asyncio.CancelledError:
        try:
            await _fallback_persist(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
            )
        except Exception:
            logger.exception("Fallback persistence retry failed")


async def _shielded_log_save(extra_on_result, res: _PipelineResult, asst_id: int | None):
    """Run the ``extra_on_result`` callback exactly once under ``asyncio.shield``.

    The callback writes a ``conversation_logs`` row, which is a bare INSERT
    with no dedup guard. Unlike :func:`_shielded_fallback`, cancellation is
    not retried — a partial write has already committed the row once, and
    re-running it would create a duplicate. Non-cancel errors are swallowed
    so a log failure never crashes the turn.

    Called from the ``finally`` block of :func:`_consume_pipeline`.
    """

    async def _run():
        try:
            await extra_on_result(res, asst_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to save conversation log")

    await asyncio.shield(_run())


async def _consume_pipeline(
    pipeline: AsyncIterator[dict],
    conversation_id: str,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    *,
    extra_on_result=None,
) -> AsyncIterator[dict]:
    """Drain the pipeline's SSE events, persist results, and emit ``done``.

    Passes ``token`` and all other public events straight to the caller.
    When the ``_result`` event arrives, persists the assistant message and
    calls the optional *extra_on_result* callback ``(res, asst_id) -> None``
    (used by every entry point to write the conversation log).

    Falls back to partial persistence in the ``finally`` block if the pipeline
    exits before ``_result`` fires (abort or error).

    Called by :func:`_generate_reply`.
    """
    res = _PipelineResult()
    asst_id = None
    persisted = False
    accumulated_text = ""

    try:
        async for event in pipeline:
            etype = event["event"]
            if etype == "token":
                accumulated_text += event["data"]
                yield event
            elif etype == "_result":
                res = _PipelineResult(**event["data"])
                asst_id, rejected = await _persist_result(conversation_id, res, settings, user_msg_id, turn_index)
                persisted = True
                if rejected and asst_id is not None:
                    # originating_attachment_id is None (first-write rejection, no DB row).
                    yield {
                        "event": "workflow_attachments_rejected",
                        "data": {
                            "message_id": asst_id,
                            "rejected": [
                                {
                                    "filename": a.get("filename"),
                                    "workflow_id": a.get("workflow_id"),
                                    "mime": a.get("mime"),
                                    "reason": a.get("reason") or OVERSIZE_NO_METADATA_REASON,
                                    "originating_attachment_id": None,
                                }
                                for a in rejected
                            ],
                        },
                    }
            else:
                yield event
    finally:
        # Runs on every exit path (normal, exception, cancellation) exactly once.
        if not persisted:
            await _shielded_fallback(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
            )
        elif extra_on_result:
            await _shielded_log_save(extra_on_result, res, asst_id)

    yield {"event": "done"}


async def _generate_reply(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    pipeline_settings: Mapping[str, Any],
    last_user_message: str,
    lorebook_messages: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Sequence[Mapping[str, Any]],
    user_msg_id: int | None,
    asst_turn_index: int,
    log_turn_index: int,
    editor_audit_msgs: list[str] | None = None,
    consume_settings: Mapping[str, Any] | None = None,
) -> AsyncIterator[dict]:
    """Run the full turn (setup → pipeline → persist) and stream all SSE events.

    The user message row must already be persisted before this is called.
    Calls :func:`_prepare_turn`, :func:`_run_pipeline`, and
    :func:`_consume_pipeline` in sequence.

    *pipeline_settings* drives the passes; *consume_settings* (defaults to the
    same) controls what settings are used during persistence — they differ only
    for super-regenerate, which passes a rewrite-disabled copy to the pipeline
    but persists under the original settings. *user_message* is what the writer
    actually receives, which may differ from *last_user_message* (e.g.
    super-regenerate uses an OOC steering message as the writer input).

    Called by every public entry point that generates a reply.
    """
    setup: _TurnSetup | None = None
    async for ev in _prepare_turn(
        ctx,
        conversation_id,
        history=history,
        settings=pipeline_settings,
        last_user_message=last_user_message,
        lorebook_messages=lorebook_messages,
    ):
        if isinstance(ev, _TurnSetup):
            setup = ev
        else:
            yield ev
    assert setup is not None

    pipeline = _run_pipeline(
        ctx.client,
        pipeline_settings,
        ctx.director,
        ctx.mood_fragments,
        ctx.interactive_fragments,
        user_message,
        attachments=attachments,
        phrase_bank=ctx.phrase_bank,
        lorebook_block=setup.lorebook_block,
        lorebook_catalog=setup.lorebook_catalog,
        agentic_lorebook=setup.agentic_lorebook_active,
        lorebook_entries=ctx.lorebook_entries,
        editor_audit_msgs=editor_audit_msgs,
        agent_client=ctx.agent_client,
        agent_prefix=setup.agent_prefix,
        macros=setup.macros,
        conversation_id=conversation_id,
        character_id=ctx.conv.get("character_card_id"),
        card=ctx.card,
        prefix=setup.prefix,
        enabled_tools=setup.merged_enabled_tools,
        turn_scratch=setup.turn_scratch,
        kv_tracker=setup.kv_tracker,
        schema_overrides=setup.schema_overrides,
        history=history,
        lorebook_messages=lorebook_messages,
    )
    async for event in _consume_pipeline(
        pipeline,
        conversation_id,
        consume_settings if consume_settings is not None else pipeline_settings,
        user_msg_id,
        asst_turn_index,
        extra_on_result=_conversation_log_writer(conversation_id, log_turn_index),
    ):
        yield event


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_turn(
    conversation_id: str,
    user_message: str,
    skip_user_persist: bool = False,
    attachments: Optional[List[dict]] = None,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Handle a new user message: save it, run the pipeline, stream the reply.

    The main entry point for ``POST /conversations/{cid}/send`` and
    ``POST /conversations/{cid}/continue``. For ``/continue``
    (``skip_user_persist=True``) the user row already exists as the last
    message; the pipeline runs from there without creating a duplicate row.

    Streams SSE events: ``user_message_created``, then all pipeline events
    (``director_done``, ``token``, ``editor_done``, etc.), and finally ``done``.
    """
    try:
        if attachments is None:
            attachments = []
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx.settings
        messages = await db.get_messages(conversation_id)
        conv = ctx.conv

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        # For /continue the user row already exists; use its turn_index.
        user_turn = next_turn

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]
            user_turn = messages[-1]["turn_index"]

        # Read progressive_fields from the grandparent node (branch-aware, unlike conversation_logs).
        grandparent = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        ctx.director["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}

        if not skip_user_persist:
            # Normalize frontend attachment format to DB format before persisting.
            db_attachments = []
            for att in attachments:
                db_attachments.append(
                    {
                        "mime_type": att.get("mime", att.get("mime_type", "image/jpeg")),
                        "data_b64": att.get("b64", att.get("data_b64", "")),
                        "filename": att.get("filename"),
                        "size": att.get("size"),
                    }
                )
            user_msg_id, _ = await db.add_message(
                conversation_id,
                "user",
                user_message,
                next_turn,
                parent_id=user_parent_id,
                attachments=db_attachments,
            )
            await db.set_active_leaf(conversation_id, user_msg_id)
            yield {"event": "user_message_created", "data": {"id": user_msg_id}}

        asst_turn = user_turn + 1

        # Include the current user message in lorebook scan, not just history.
        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            pipeline_settings=settings,
            last_user_message=user_message,
            lorebook_messages=history + [{"role": "user", "content": user_message}],
            user_message=user_message,
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=asst_turn,
            log_turn_index=user_turn,
        ):
            yield event

    except Exception:
        logger.exception("Pipeline error")
        yield {"event": "error", "data": "Generation failed; see server logs"}


async def handle_fork_edit(
    conversation_id: str,
    user_msg_id: int,
    new_content: str,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Fork the conversation at a user message: save an edited sibling and generate a fresh reply.

    Entry point for ``POST /messages/{id}/fork-edit``. Persists the edited text
    as a new sibling of *user_msg_id* (same parent and turn index), resets the
    director to the branch point, then runs the full pipeline to produce a new
    reply. The original message and its subtree are left intact; branch
    navigation then shows both versions.

    Logs at the assistant turn (not the user turn) so this branch's log row is
    distinct from the original turn's log at the user turn.
    """
    try:
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx.settings
        original = await db.get_message_by_id(user_msg_id)
        if not original or original["conversation_id"] != conversation_id or original["role"] != "user":
            yield {"event": "error", "data": "Invalid target message"}
            return

        parent_id: int | None = original["parent_id"]
        turn_index = original["turn_index"]
        asst_turn = turn_index + 1
        history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []

        # Reset director to branch-point baseline (branch-aware progressive_fields).
        ctx.director["active_moods"] = await db.get_moods_before_turn(conversation_id, turn_index)
        grandparent = next((m for m in reversed(history) if m["role"] == "assistant"), None)
        ctx.director["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}

        # Carry original attachments onto the new sibling.
        carried_atts = await db.get_user_attachments_for_message(user_msg_id)

        new_user_id, _ = await db.add_message(
            conversation_id,
            "user",
            new_content,
            turn_index,
            parent_id=parent_id,
            attachments=carried_atts,
        )
        await db.set_active_leaf(conversation_id, new_user_id)
        yield {"event": "user_message_created", "data": {"id": new_user_id}}

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            pipeline_settings=settings,
            last_user_message=new_content,
            lorebook_messages=history + [{"role": "user", "content": new_content}],
            user_message=new_content,
            attachments=carried_atts,
            user_msg_id=new_user_id,
            asst_turn_index=asst_turn,
            log_turn_index=asst_turn,  # log at assistant turn, unlike handle_turn
        ):
            yield event

    except Exception:
        logger.exception("Fork edit error")
        yield {"event": "error", "data": "Generation failed; see server logs"}


async def handle_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Regenerate an existing assistant message as a new sibling branch.

    Entry point for ``POST /messages/{id}/regenerate``. Resets the director to
    the pre-turn baseline and re-runs the full pipeline from the parent user
    message, producing a new reply at the same turn index. The original message
    is kept; branch navigation shows both.
    """
    try:
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx.settings
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            pipeline_settings=settings,
            last_user_message=user_msg["content"],
            lorebook_messages=[
                *history,
                {"role": "user", "content": user_msg["content"]},
            ],
            user_message=user_msg["content"],
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=target["turn_index"],
            log_turn_index=target["turn_index"],
        ):
            yield event

    except Exception:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": "Generation failed; see server logs"}


_SUPER_REGEN_MSG = "[OOC: Your response was kind of meh, rewrite it in a slightly different but still realistic direction.]"


async def handle_super_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Regenerate a reply with the original exchange kept as context (super-regenerate).

    Entry point for ``POST /messages/{id}/super_regenerate``. Extends history to
    include the original user + assistant exchange so the model sees what it
    previously wrote, then sends an OOC steering message asking for a different
    direction. The rewrite tool is disabled to prevent the director from altering
    that steering message. The result is saved as a new sibling branch.
    """
    try:
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx.settings
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)

        # Include the original exchange so the model sees what it wrote before being steered.
        extended_history = [
            *history,
            {"role": "user", "content": user_msg["content"]},
            {"role": "assistant", "content": target["content"]},
        ]
        super_regen_settings = {
            **settings,
            "enabled_tools": disable_rewrite(settings.get("enabled_tools") or {}),
        }

        # Exclude target content from audit so the new draft isn't penalised for repeating it.
        editor_audit_msgs = [msg["content"] for msg in reversed(history) if msg.get("role") == "assistant"][:3]

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=extended_history,
            pipeline_settings=super_regen_settings,
            last_user_message=user_msg["content"],
            lorebook_messages=extended_history,
            user_message=_SUPER_REGEN_MSG,
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=target["turn_index"],
            log_turn_index=target["turn_index"],
            editor_audit_msgs=editor_audit_msgs,
            consume_settings=settings,
        ):
            yield event

    except Exception:
        logger.exception("Super-regenerate error")
        yield {"event": "error", "data": "Generation failed; see server logs"}


async def handle_magic_rewrite(
    conversation_id: str,
    assistant_msg_id: int,
    direction: str,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Rewrite an assistant message in place following a user-supplied direction.

    Entry point for ``POST /messages/{id}/magic_rewrite``. Appends the original
    exchange to history, then runs a single writer-style LLM call (no director
    or editor passes) with an OOC instruction built from *direction*. Uses the
    same writer lane and tool blob as a normal turn so the LLM's KV cache is
    reused. On success, overwrites the stored message content; on abort, the
    original is left unchanged.
    """
    try:
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        settings = ctx.settings
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        parent_id: int | None = user_msg.get("parent_id")
        history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []

        extended_history = [
            *history,
            {"role": "user", "content": user_msg["content"]},
            {"role": "assistant", "content": target["content"]},
        ]
        prefix = _build_prefix_from_ctx(ctx, extended_history)

        direction_msg = f"[OOC: Rewrite the above response. Direction: {direction}]"

        # Use the writer lane so the tool blob is byte-identical to normal turns
        # (single-model ships the shared schema; dual-model drops tools per Invariant 5).
        macros = Macros.from_settings(settings, ctx.conv["character_name"], ctx.active_persona)
        enabled_tools_setting = settings.get("enabled_tools") or {}
        enabled_tools = dict(enabled_tools_setting) if agent_enabled(settings) else {k: False for k in enabled_tools_setting}
        agentic_active = _agentic_lorebook_active(
            settings, enabled_tools, ctx.lorebook_entries, agent_on=agent_enabled(settings)
        )
        schema_overrides = _build_writer_tools_blob(
            settings, ctx.interactive_fragments, enabled_tools, agentic_lorebook=agentic_active
        )
        cfg = _resolve_pipeline_config(
            settings,
            enabled_tools,
            macros=macros,
            client=ctx.client,
            agent_client=ctx.agent_client,
            agent_prefix=None,  # agent lane is unused by the rewrite
            prefix=prefix,
            phrase_bank=ctx.phrase_bank,
            schema_overrides=schema_overrides,
        )
        writer_lane = cfg.writer_lane

        hyperparams = extract_hyperparams(settings)

        writer_reasoning_on = bool((settings.get("reasoning_enabled_passes") or {}).get("writer", False))
        extra = reasoning_cfg(writer_reasoning_on)

        kv_tracker = _KVCacheTracker(conversation_id=conversation_id)
        accumulated = ""
        async for item in writer_lane.base.complete(
            writer_lane.client,
            label="magic_rewrite",
            trailing=[{"role": "user", "content": direction_msg}],
            # Empty in dual-model (no tools); otherwise prevent the model from calling any.
            tool_choice="none" if writer_lane.base.tools else None,
            kv_tracker=kv_tracker,
            **extra,
            **hyperparams,
        ):
            if item["type"] == "done":
                break
            if item["type"] == "reasoning":
                yield {
                    "event": "reasoning",
                    "data": {"pass": "writer", "delta": item["delta"]},
                }
            elif item["type"] == "content":
                accumulated += item["delta"]
                yield {"event": "token", "data": item["delta"]}

        kv_tracker.log_summary()

        # Don't overwrite on abort; keep the original message.
        if accumulated.strip() and not ctx.client.is_aborted:
            await db.update_message_content(assistant_msg_id, accumulated)

        yield {"event": "done"}

    except Exception:
        logger.exception("Magic rewrite error")
        yield {"event": "error", "data": "Generation failed; see server logs"}
