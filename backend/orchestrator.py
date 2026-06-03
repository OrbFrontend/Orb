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

from . import database as db
from .llm_client import LLMClient, reasoning_cfg
from .endpoint_profiles import profile_for
from .tool_defs import TOOLS, POST_WRITER_TOOLS, build_direct_scene_tool
from .prompt_builder import (
    build_prefix,
    compute_style_injection_block,
    compute_lorebook_injection_block,
)
from .kv_tracker import _KVCacheTracker
from .locks import workflow_character_state_lock, workflow_state_lock
from .macros import Macros
from .workflows import (
    HookType,
    PostCtx,
    PreCtx,
    _readonly,
    get_workflow,
    iter_subscriptions,
)
from .workflows.attachment_cache import OVERSIZE_NO_METADATA_REASON
from .llm_types import ChatMessage
from .utils import LengthGuard, extract_hyperparams
from .passes.director import DirectorResult, _director_pass
from .passes.writer import _writer_pass, build_writer_content
from .passes.editor import editor_pass
from .database.models import (
    CharacterCardRow,
    ConversationRow,
    DirectorFragmentRow,
    DirectorStateRow,
    LorebookEntryRow,
    MoodFragmentRow,
    PhraseGroup,
    SettingsRow,
    UserPersonaRow,
)

logger = logging.getLogger(__name__)


# ── Core pipeline ─────────────────────────────────────────────────────────────


@dataclass
class _PipelineConfig:
    """Resolved per-turn flags, clients, and prefixes for :func:`_run_pipeline`.

    A pure derivation from *settings* plus the (possibly agent-zeroed)
    enabled-tools map; it holds no mutable turn state. ``enabled_tools`` is the
    final map with ``editor_rewrite`` folded in whenever the length guard is
    active, so ``enabled_schemas()`` ships an identical tool blob across all
    three passes (the KV-cache invariant).
    """

    agent_on: bool
    enabled_tools: Mapping[str, bool]
    director_reasoning_on: bool
    writer_reasoning_on: bool
    editor_reasoning_on: bool
    audit_enabled: bool
    length_guard_enabled: bool
    length_guard_enforce: bool
    length_guard: LengthGuard | None
    do_edit: bool
    writer_enabled_tools: Mapping[str, bool]
    director_client: LLMClient
    writer_client: LLMClient
    editor_client: LLMClient
    director_prefix: list[ChatMessage]
    editor_prefix: list[ChatMessage]
    agent_model: str


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
) -> _PipelineConfig:
    """Derive the per-turn pipeline configuration (see :class:`_PipelineConfig`)."""
    agent_on = bool(settings.get("enable_agent", 1))
    reasoning_passes = settings.get("reasoning_enabled_passes") or {}

    audit_enabled = agent_on and bool(enabled_tools.get("editor_apply_patch", False)) and phrase_bank is not None

    length_guard_enabled = bool(settings.get("length_guard_enabled", 0)) if agent_on else False
    # The length-guard *feature* requires the editor_rewrite *tool*: mirror it into
    # enabled_tools so enabled_schemas() includes its schema in all three passes —
    # the same KV-cache approach as editor_apply_patch. editor_rewrite is internal
    # (not user-toggleable); this feature flag is its only enable path.
    if length_guard_enabled:
        enabled_tools = {**enabled_tools, "editor_rewrite": True}
    length_guard_enforce = bool(settings.get("length_guard_enforce", 0)) if agent_on else False

    # length_guard_enabled already folds in agent_on (it is False whenever the
    # agent is off), so no extra `and agent_on` guard is needed below.
    length_guard: LengthGuard | None = (
        {
            "enabled": length_guard_enabled,
            "max_words": int(settings.get("length_guard_max_words", 240)),
            "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
        }
        if length_guard_enabled
        else None
    )

    # When the agent runs on a separate model/endpoint its KV cache is disjoint
    # from the writer's; skip tool schemas and the OOC "no tools" notice from the
    # writer call — neither is useful and both add unnecessary tokens.
    writer_enabled_tools = {} if agent_client is not None else enabled_tools

    return _PipelineConfig(
        agent_on=agent_on,
        enabled_tools=enabled_tools,
        director_reasoning_on=bool(reasoning_passes.get("director", True)),
        writer_reasoning_on=bool(reasoning_passes.get("writer", False)),
        editor_reasoning_on=bool(reasoning_passes.get("editor", False)),
        audit_enabled=audit_enabled,
        length_guard_enabled=length_guard_enabled,
        length_guard_enforce=length_guard_enforce,
        length_guard=length_guard,
        do_edit=audit_enabled or length_guard_enabled,
        writer_enabled_tools=writer_enabled_tools,
        director_client=macros.wrap_client(agent_client or client),
        writer_client=macros.wrap_client(client),
        editor_client=macros.wrap_client(agent_client or client),
        director_prefix=agent_prefix or prefix,
        editor_prefix=agent_prefix or prefix,
        agent_model=(settings.get("agent_model_name", settings["model_name"]) if agent_client else settings["model_name"]),
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
    staged_attachments: list[dict] = field(default_factory=list)
    staged_message_state: dict = field(default_factory=dict)

    def as_event_data(self) -> dict:
        """Shallow field dict for the ``_result`` SSE envelope. Shallow on
        purpose: ``staged_attachments`` carries raw artifact bytes that must not
        be deep-copied."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


async def _run_pipeline(
    client: LLMClient,
    settings: Mapping[str, Any],
    director: Mapping[str, Any],
    mood_fragments: Sequence[Mapping[str, Any]],
    director_fragments: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Optional[Sequence[Mapping[str, Any]]] = None,
    phrase_bank: list[PhraseGroup] | None = None,
    lorebook_block: str = "",
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
) -> AsyncIterator[dict]:
    """Three-pass pipeline: director → writer → editor, plus a post-pipeline
    workflow iteration before persistence.

    KV cache strategy: *prefix* (system prompt + chat history) and the tool
    schema list returned by ``enabled_schemas(enabled_tools, schema_overrides)``
    are kept byte-identical across all three passes so the LLM can reuse cached
    KV entries. ``direct_scene`` is dynamic per character; its schema is built
    once by the caller via ``build_direct_scene_tool(director_fragments)`` and
    threaded as ``schema_overrides`` to every pass so the tools blob matches.
    Only ``tool_choice`` and the trailing user message differ per pass.
    ``editor_rewrite`` is included in the schema set whenever the length guard
    is enabled (mirroring how ``editor_apply_patch`` tracks ``audit_enabled``).

    *enabled_tools* is already merged (settings plus any pre-pipeline
    contribution, with the enable_agent zeroing already applied) and *prefix*
    is the final pipeline prefix including any extra system blocks --
    construction lives in the caller so the pre-pipeline iteration site can
    yield its own SSE events before the pipeline starts. *turn_scratch* is the
    per-turn workflow scratch dict, ref-shared with every workflow hook this
    turn. *kv_tracker* is likewise constructed by the caller and finalised
    here via ``log_summary()`` after the post-pipeline loop closes.
    *schema_overrides* is the per-turn dynamic-schema map the caller builds
    (today: ``{"direct_scene": build_direct_scene_tool(director_fragments)}``)
    and threads here so every pass receives it for byte-identical tools.
    *history* is the prior-message list forwarded read-only onto each
    ``PostCtx``; the passes read history through *prefix*, not this argument.

    The single ``_result`` event fires after the post-pipeline iteration and
    carries both the final draft and any workflow-staged attachments.
    """
    if macros is None:
        macros = Macros("User", "")
    if attachments is None:
        attachments = []

    user_message = macros.resolve_message(user_message)

    # Resolved per-turn config; referenced as cfg.* throughout so each use site
    # reads as immutable derived config, distinct from the mutable turn state
    # below. cfg.enabled_tools is the length-guard-folded map (not the bare
    # *enabled_tools* parameter), so the pipeline reads tools through cfg.
    cfg = _resolve_pipeline_config(
        settings,
        enabled_tools,
        macros=macros,
        client=client,
        agent_client=agent_client,
        agent_prefix=agent_prefix,
        prefix=prefix,
        phrase_bank=phrase_bank,
    )

    # Mutable turn state, accumulated as the three passes run below.
    active_moods = director["active_moods"]
    _valid_progressive_ids = {df["id"] for df in director_fragments if df.get("field_type") == "progressive"}
    progressive_state: dict = {k: v for k, v in director.get("progressive_fields", {}).items() if k in _valid_progressive_ids}
    agent_raw, calls, latency = "", [], 0
    rewritten_msg: str | None = None
    extra_fields: dict = {}
    reasoning_director_text = ""
    reasoning_writer_text = ""
    reasoning_editor_text = ""
    progressive_fields: dict = {}
    effective_msg = user_message

    # --- Director pass ---
    has_pre_writer_tools = any(cfg.enabled_tools.get(n, False) for n in TOOLS if n not in POST_WRITER_TOOLS)
    if cfg.agent_on and has_pre_writer_tools:
        yield {"event": "director_start"}
        async for event in _director_pass(
            cfg.director_client,
            cfg.director_prefix,
            user_message,
            settings,
            director,
            mood_fragments,
            director_fragments,
            cfg.enabled_tools,
            attachments=attachments,
            kv_tracker=kv_tracker,
            reasoning_on=cfg.director_reasoning_on,
            lorebook_block=lorebook_block,
            model=cfg.agent_model,
            progressive_state=progressive_state,
            schema_overrides=schema_overrides,
        ):
            if event["type"] == "reasoning":
                reasoning_director_text += event["delta"]
                yield {
                    "event": "reasoning",
                    "data": {"pass": "director", "delta": event["delta"]},
                }
            elif event["type"] == "done":
                result: DirectorResult = event["result"]
                active_moods = result.active_moods
                agent_raw = result.agent_raw
                calls = result.calls
                latency = result.latency
                rewritten_msg = result.rewritten_msg
                extra_fields = result.extra_fields
                progressive_fields = {k: v for k, v in extra_fields.items() if k in _valid_progressive_ids}
        if rewritten_msg:
            effective_msg = rewritten_msg
            yield {
                "event": "prompt_rewritten",
                "data": {"refined_message": rewritten_msg},
            }

    # Bail out if stop was clicked during the director pass
    if client.is_aborted or (agent_client is not None and agent_client.is_aborted):
        return

    # Style injection
    direct_scene_enabled = cfg.agent_on and bool(cfg.enabled_tools.get("direct_scene", False))
    inj_block = macros.resolve_message(
        compute_style_injection_block(
            active_moods,
            director["active_moods"],
            mood_fragments,
            director_fragments,
            direct_scene_enabled,
            extra_fields,
            progressive_state,
        )
    )

    yield {
        "event": "director_done",
        "data": {
            "active_moods": active_moods,
            "injection_block": inj_block,
            "tool_calls": calls,
            "agent_latency_ms": latency,
            "extra_fields": extra_fields,
        },
    }

    # --- Writer pass ---
    writer_content = build_writer_content(
        lorebook_block,
        inj_block,
        cfg.writer_enabled_tools,
        effective_msg,
        attachments,
        cfg.length_guard_enforce,
        cfg.length_guard,
    )
    resp_text = ""
    async for item in _writer_pass(
        cfg.writer_client,
        prefix,
        settings,
        cfg.writer_enabled_tools,
        inj_block=inj_block,
        lorebook_block=lorebook_block,
        effective_msg=effective_msg,
        attachments=attachments,
        length_guard_enforce=cfg.length_guard_enforce,
        length_guard=cfg.length_guard,
        kv_tracker=kv_tracker,
        reasoning_on=cfg.writer_reasoning_on,
        schema_overrides=schema_overrides,
    ):
        if item["type"] == "reasoning":
            reasoning_writer_text += item["delta"]
            yield {
                "event": "reasoning",
                "data": {"pass": "writer", "delta": item["delta"]},
            }
        else:
            resp_text += item["delta"]
            yield {"event": "token", "data": item["delta"]}

    def _make_result(final_text: str, staged: list[dict], staged_state: dict | None = None) -> dict:
        return {
            "event": "_result",
            "data": _PipelineResult(
                active_moods=active_moods,
                agent_raw=agent_raw,
                calls=calls,
                latency=latency,
                rewritten_msg=rewritten_msg,
                effective_msg=effective_msg,
                resp_text=final_text,
                inj_block=inj_block,
                extra_fields=extra_fields,
                progressive_fields=progressive_fields,
                reasoning_director=reasoning_director_text,
                reasoning_writer=reasoning_writer_text,
                reasoning_editor=reasoning_editor_text,
                staged_attachments=staged,
                staged_message_state=staged_state or {},
            ).as_event_data(),
        }

    # If the turn was aborted during writer, persist what streamed so far and
    # skip the editor + post-pipeline iteration. The single _result still
    # fires so the persistence path stays uniform.
    if client.is_aborted or (agent_client is not None and agent_client.is_aborted):
        yield _make_result(resp_text, [])
        kv_tracker.log_summary()
        return

    # --- Editor pass ---
    if cfg.do_edit and resp_text:
        logger.info(
            "Editor pass starting (draft=%d chars, phrase_bank=%d groups)",
            len(resp_text),
            len(phrase_bank) if phrase_bank else 0,
        )
        try:
            async for event in editor_pass(
                cfg.editor_client,
                cfg.editor_prefix,
                effective_msg,
                resp_text,
                settings,
                phrase_bank or [],
                cfg.enabled_tools,
                cfg.audit_enabled,
                cfg.length_guard,
                kv_tracker=kv_tracker,
                reasoning_on=cfg.editor_reasoning_on,
                audit_context_msgs=editor_audit_msgs,
                model=cfg.agent_model,
                writer_user_msg=writer_content,
                schema_overrides=schema_overrides,
            ):
                if event["type"] == "reasoning":
                    reasoning_editor_text += event["delta"]
                    yield {
                        "event": "reasoning",
                        "data": {"pass": "editor", "delta": event["delta"]},
                    }
                elif event["type"] == "done":
                    refined_draft = event["draft"]
                    if refined_draft and refined_draft != resp_text:
                        resp_text = refined_draft
                        yield {
                            "event": "writer_rewrite",
                            "data": {"refined_text": resp_text},
                        }
                    if event.get("tool_calls"):
                        yield {
                            "event": "editor_done",
                            "data": {"tool_calls": event["tool_calls"]},
                        }
        except Exception as e:
            logger.error("editor pass failed, keeping original: %s", e, exc_info=True)
    else:
        logger.info("Editor pass skipped (do_edit=%s, draft=%d chars)", cfg.do_edit, len(resp_text))

    # --- Post-pipeline workflow iteration ---
    # PostCtx.director_output is a read-only mapping (workflow contract), so this
    # stays a dict rather than the DirectorResult dataclass. Keys mirror the
    # dataclass field names — notably ``agent_raw`` (not ``raw``) — so a single
    # name follows each value from the director pass through to every consumer.
    director_output = {
        "active_moods": active_moods,
        "agent_raw": agent_raw,
        "calls": calls,
        "latency": latency,
        "rewritten_msg": rewritten_msg,
        "extra_fields": extra_fields,
        "progressive_fields": progressive_fields,
    }
    post: _PostPipelineResult | None = None
    async for ev in _run_post_pipeline(
        draft=resp_text,
        conversation_id=conversation_id,
        character_id=character_id,
        card=card,
        history=history,
        effective_msg=effective_msg,
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

    yield _make_result(post.draft, post.staged_attachments, post.staged_message_state)
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
    """Drive each POST_PIPELINE subscription over the finished *draft*, streaming
    pass-through SSE events and yielding one terminal :class:`_PostPipelineResult`.

    Each hook may yield ``draft_replaced`` (one applied per hook per turn) to
    mutate the draft for downstream hooks and final persistence, plus zero or
    more ``attach_artifact`` entries that are validated, path-normalized, and
    staged for the upcoming add_message transaction, and ``set_message_state``
    slots written once the assistant row id is known. Per-workflow exceptions are
    logged-and-skipped; one bad hook does not crash a turn.
    """
    staged_attachments: list[dict] = []
    staged_message_state: dict[str, dict] = {}
    for sub in iter_subscriptions(HookType.POST_PIPELINE):
        replaced_this_hook = False
        # Serialize same-(cid, workflow_id) writers against concurrent
        # /trigger calls and any other in-flight pipeline that reaches this
        # hook on the same conversation. Different workflows on the same
        # conversation keep distinct lock keys, so they still run in parallel.
        async with workflow_state_lock(conversation_id or "", sub.workflow_id), workflow_character_state_lock(
            character_id or "", sub.workflow_id
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
                        yield {"event": "writer_rewrite", "data": {"refined_text": draft}}
                        continue
                    if t == "attach_artifact":
                        # Only workflows declared with produces_artifacts=True
                        # may persist attachments. Drop silently otherwise so a
                        # misconfigured workflow cannot corrupt the turn.
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
                        # The assistant row is minted in _persist_result after
                        # this loop, so the slot is written there. The owning
                        # workflow comes from the subscription, so a hook can
                        # only address its own slot.
                        state = ev.get("state") if isinstance(ev, dict) else None
                        if not isinstance(state, dict):
                            logger.warning(
                                "post_pipeline hook %r yielded set_message_state with " "non-dict state (type=%s); ignoring",
                                sub.workflow_id,
                                type(state).__name__,
                            )
                            continue
                        staged_message_state[sub.workflow_id] = state
                        continue
                    # Underscore-prefixed event names are reserved by
                    # _consume_pipeline for internal persistence signals
                    # (_result). Refuse them here so a hook cannot
                    # drive an extra db.add_message or rewrite of the assistant
                    # row via impersonation.
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
    """Validate a workflow ``attach_artifact`` entry and normalize it to the
    bytes-only shape ``add_message`` consumes.

    Returns the staged dict on acceptance, or ``None`` (with a logged warning)
    when validation fails. Never raises -- workflow noise must not crash a
    turn. The orchestrator owns this validation so the DB layer can trust
    incoming staged attachments and write them in the same transaction as the
    parent message row.
    """
    if not isinstance(att, dict):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with non-dict " "attachment (type=%s); ignoring",
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
    # Whitespace-only annotation collapses to None so the history renderer
    # sees one sentinel for "no LLM-visible footprint".
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
                "post_pipeline hook %r yielded attach_artifact with path=%r " "that failed to read (%s); dropping entry",
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
            "post_pipeline hook %r yielded attach_artifact with empty data " "(filename=%r); dropping entry",
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
    """Drive each pre_pipeline subscription in priority-ascending order,
    ties broken by registration order. Yields SSE pass-through events;
    mutates *accumulators* with the merged enable_tools map and the
    system_prompt block list.

    *accumulators* must enter populated with
    ``{"merged_enabled_tools": <fresh dict>, "extras": []}``; this helper
    updates both in place. PreCtx construction is inside the try block so a
    ``_readonly`` failure on pathological input is logged-and-skipped rather
    than crashing the turn. *schema_overrides* carries the per-turn
    dynamic-schema map the pipeline ships to ``enabled_schemas(...)``; it is
    ref-shared with every PreCtx so workflow hooks issuing forced calls can
    pass it through to ``forced_tool_call`` for byte-identical cache reuse.
    """
    for sub in iter_subscriptions(HookType.PRE_PIPELINE):
        # See workflow_state_lock invariant in backend/locks.py: held across the
        # hook's full lifetime to keep its workflow_state RMW atomic against any
        # concurrent /trigger or pipeline iteration on the same (cid, wid).
        async with workflow_state_lock(conversation_id, sub.workflow_id), workflow_character_state_lock(
            character_id or "", sub.workflow_id
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
                                "pre_pipeline hook %r yielded enable_tools with " "invalid tools payload (type=%s); ignoring",
                                sub.workflow_id,
                                type(tools).__name__,
                            )
                            continue
                        for name, val in items:
                            if val is not True:
                                logger.warning(
                                    "workflow %r yielded enable_tools %r=%r; only True " "is honored, entry dropped",
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
                                "pre_pipeline hook %r yielded empty/whitespace-only " "system_prompt; ignoring",
                                sub.workflow_id,
                            )
                            continue
                        accumulators["extras"].append(block)
                        continue
                    # Reserved by _consume_pipeline; refused here as
                    # defense-in-depth even though pre_pipeline events do not
                    # currently flow through that consumer.
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
    director_fragments: list[DirectorFragmentRow]
    phrase_bank: list[PhraseGroup]
    lorebook_entries: list[LorebookEntryRow]
    client: LLMClient
    system_prompt: str
    char_persona: str
    mes_example: str
    active_persona: Optional[UserPersonaRow]
    agent_client: Optional[LLMClient]
    agent_system_prompt: Optional[str]


async def _load_pipeline_context(conversation_id: str) -> PipelineContext | None:
    """Load everything the pipeline needs: settings, conversation, director,
    mood_fragments, phrase_bank, and an LLMClient.

    Returns a :class:`PipelineContext`, or None if the conversation was not found.
    """
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director = await db.get_director_state(conversation_id)
    mood_fragments = await db.get_mood_fragments()
    # Filter out disabled mood fragments
    mood_fragments = [f for f in mood_fragments if f.get("enabled", True)]
    # Remove disabled mood fragments from active moods
    if director and director.get("active_moods"):
        enabled_ids = {f["id"] for f in mood_fragments}
        director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
    director_fragments = await db.get_director_fragments()
    director_fragments = [df for df in director_fragments if df.get("enabled", True)]
    phrase_bank = await db.get_phrase_bank()
    lorebook_entries = await db.get_active_lorebook_entries()
    client = LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        profile=profile_for(
            settings["endpoint_url"],
            settings.get("model_name", ""),
        ),
    )

    card_id = conv.get("character_card_id")
    card = await db.get_character_card(card_id) if card_id else None
    system_prompt, char_persona, mes_example = await db.resolve_char_context(conv, settings, card=card)

    # Load active persona if set
    active_persona = None
    active_persona_id = settings.get("active_persona_id")
    if active_persona_id:
        active_persona = await db.get_user_persona(active_persona_id)

    # Resolve agent client and system prompt when separate from writer
    agent_same = settings.get("agent_same_as_writer", True)
    agent_client = None
    agent_system_prompt = None
    if not agent_same and settings.get("agent_endpoint_id"):
        agent_url = settings.get("agent_endpoint_url", settings["endpoint_url"])
        agent_api_key = settings.get("agent_api_key", settings.get("api_key", ""))
        agent_model = settings.get("agent_model_name", settings.get("model_name", ""))
        agent_client = LLMClient(
            agent_url,
            api_key=agent_api_key,
            profile=profile_for(agent_url, agent_model),
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
        director_fragments=director_fragments,
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
    """Build the LLM prefix from a :class:`PipelineContext`.

    When *system_prompt* is provided it overrides ``ctx.system_prompt``
    (used for the agent prefix when it has its own system prompt).
    *extra_system_blocks* appends contributions from pre-pipeline hooks; None
    or an empty list preserves baseline byte parity.
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
    """Build (prefix, agent_prefix) from *ctx* and *history*.

    *agent_prefix* is ``None`` when no separate agent system prompt is
    configured. *extra_system_blocks* is applied to both prefixes so the
    system body stays identical across director / writer / editor.
    """
    prefix = _build_prefix_from_ctx(ctx, history, extra_system_blocks=extra_system_blocks)
    agent_sp = ctx.agent_system_prompt
    agent_prefix = (
        _build_prefix_from_ctx(ctx, history, system_prompt=agent_sp, extra_system_blocks=extra_system_blocks)
        if agent_sp is not None
        else None
    )
    return prefix, agent_prefix


def _compute_lorebook(macros: Macros, ctx: PipelineContext, messages: Sequence[Mapping[str, Any]]) -> str:
    """Compute the lorebook injection block for a sequence of *messages*."""
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


async def _prepare_turn(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    last_user_message: str,
    lorebook_messages: Sequence[Mapping[str, Any]],
) -> AsyncIterator[dict | _TurnSetup]:
    """Run the turn-setup sequence shared by every entry point and stream its
    pre-pipeline SSE events, then yield one terminal :class:`_TurnSetup`.

    Drain it as::

        setup = None
        async for ev in _prepare_turn(...):
            if isinstance(ev, _TurnSetup):
                setup = ev
            else:
                yield ev
        assert setup is not None

    *history* builds the prefixes and seeds the hooks (the extended history for
    super-regenerate). *settings* is the pipeline settings whose
    ``enabled_tools`` seeds the pre-merge map and which the hooks receive
    (super-regenerate passes its rewrite-disabled copy). *last_user_message* is
    handed to the hooks; *lorebook_messages* is the full message list (history
    plus the turn's probe message) scanned for lorebook keywords. Macros and
    prefixes are always built from ``ctx.settings`` so the system body stays
    byte-identical across passes regardless of per-call setting tweaks.
    """
    macros = Macros.from_settings(ctx.settings, ctx.conv["character_name"], ctx.active_persona)
    lorebook_block = _compute_lorebook(macros, ctx, lorebook_messages)

    prefix_base, agent_prefix_base = _build_prefixes(ctx, history)

    # Per-turn shared identities — ref-shared across the hooks and every pass.
    turn_scratch: dict = {}
    kv_tracker = _KVCacheTracker(conversation_id=conversation_id)
    # Built once and never mutated for the rest of the turn -- frozen here so the
    # ref shared across every pass and hook cannot have entries added/swapped/dropped.
    # Values stay plain dicts so they remain json-serializable into the tools blob.
    schema_overrides = MappingProxyType({"direct_scene": build_direct_scene_tool(ctx.director_fragments)})

    enabled_tools_setting = settings.get("enabled_tools") or {}
    if settings.get("enable_agent", 1):
        enabled_tools_pre_merge = dict(enabled_tools_setting)
    else:
        enabled_tools_pre_merge = {k: False for k in enabled_tools_setting}
    accumulators = {
        "merged_enabled_tools": dict(enabled_tools_pre_merge),
        "extras": [],
    }

    # Workflow pre-pipeline iteration. Hooks may yield enable_tools or
    # system_prompt yields that fold into the merged tool map and the extra
    # system blocks below; any other yield passes through to the SSE stream.
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
    )


def _conversation_log_writer(conversation_id: str, log_turn_index: int):
    """Build the post-persist ``extra_on_result`` callback that writes the
    turn's ``conversation_logs`` row.

    Every entry point logs the same director/reasoning payload; they differ
    only in which ``turn_index`` the row is filed under — the user turn for a
    fresh turn, the assistant turn for the branch-creating regenerate paths
    (see ``handle_fork_edit``'s docstring for why branches log at the assistant
    turn)."""

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
        )

    return _on_result


async def _resolve_target_and_parent(
    conversation_id: str, assistant_msg_id: int
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | str:
    """Validate *assistant_msg_id* and load its parent user message.

    Returns ``(target, user_msg)`` on success, or an error string on failure.
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
    ctx: PipelineContext, conversation_id: str, target: Mapping[str, Any], user_msg: Mapping[str, Any]
) -> tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]:
    """Prepare history and attachments for a regeneration pass.

    Also resets director moods to the pre-turn baseline.
    Returns ``(history, attachments)``.
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


async def _persist_result(
    conversation_id: str,
    res: _PipelineResult,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
) -> tuple[int | None, list[dict]]:
    """Persist the assistant message after _result. Returns
    ``(asst_id, rejected_workflow_atts)``. The rejected list is empty
    when the cache accepted all workflow atts; populated when the cache
    dropped atts for rehydratability reasons (oversize without
    seed+generation_metadata). The caller (``_consume_pipeline``) emits
    a SSE event for non-empty rejections so the frontend can surface a
    warning chip on the affected message."""
    if settings.get("enable_agent", 1):
        await db.update_director_state(
            conversation_id,
            res.active_moods,
            progressive_fields=res.progressive_fields,
        )
    if res.rewritten_msg and user_msg_id:
        await db.update_message_content(user_msg_id, res.effective_msg)

    # Only create a message if there's actual content.
    # The writer pass can produce empty resp_text if the LLM completes
    # without generating any non‑reasoning tokens (e.g., reasoning‑only mode).
    resp_text = res.resp_text
    if resp_text.strip():
        # Workflow-staged attachments ride the same transaction as the row
        # INSERT so they persist iff the message persists; an aborted turn
        # that never reaches this call leaves no orphan attachment rows.
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
        # Per-workflow state staged by post-pipeline hooks targets this row,
        # whose id is only known now. The row is not yet the active leaf and
        # no other caller can name it, so each blind first write needs no lock.
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
    """Best-effort save when the pipeline aborted before _result was consumed.

    Only saves if there's actual writer output (token events). Reasoning-only
    content (director/writer/editor reasoning) does NOT create a message node,
    because reasoning deltas are yielded as 'reasoning' SSE events and are NOT
    included in accumulated_text.
    """
    try:
        if res.active_moods and settings.get("enable_agent", 1):
            await db.update_director_state(
                conversation_id,
                res.active_moods,
                progressive_fields=res.progressive_fields,
            )
        if res.rewritten_msg and user_msg_id:
            await db.update_message_content(user_msg_id, res.effective_msg)

        # Only save if there's actual writer output (token events).
        # accumulated_text only contains streamed tokens from the writer pass;
        # reasoning deltas are yielded as separate 'reasoning' events and are
        # NOT included here. This prevents creating message nodes when the
        # user stops generation during the reasoning phase (no writer output).
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
    """Run _fallback_persist inside asyncio.shield, with a retry on CancelledError."""
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
    """Run the post-persist ``extra_on_result`` callback exactly once, under
    ``asyncio.shield`` so a cancellation arriving mid-write cannot leave it
    half-done.

    ``add_conversation_log`` is a bare INSERT with no dedup, so a retry after a
    partially-committed write would duplicate the row. ``shield`` guarantees the
    inner write runs to completion even when the surrounding task is cancelled,
    so we deliberately do *not* retry on ``CancelledError`` (unlike
    ``_shielded_fallback``, whose ``add_message`` target is dedup-guarded): the
    write has already happened once, and re-running it is exactly the duplicate
    we are avoiding. Non-cancel errors are swallowed inside the shielded
    coroutine so one failed log never crashes the turn."""

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
    """Shared event-consumption loop used by both handle_turn and handle_regenerate.

    *extra_on_result* is an optional async callback ``(res, asst_id) -> None``
    that runs right after the assistant message is persisted (for handle_turn's
    conversation-log write, for example).
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
                # Rebuild the typed result from the dict-shaped SSE payload
                # (see _PipelineResult.as_event_data). The bare _PipelineResult()
                # seed above stays in place if the turn aborts before _result.
                res = _PipelineResult(**event["data"])
                asst_id, rejected = await _persist_result(conversation_id, res, settings, user_msg_id, turn_index)
                persisted = True
                if rejected and asst_id is not None:
                    # Surface the cache's rejections so the frontend can render a warning
                    # chip on the message. Bytes never enter the SSE payload. ``reason``
                    # falls back to the oversize-no-metadata constant for any legacy
                    # entry the helper failed to tag. ``originating_attachment_id`` is
                    # always None on this path because no DB row was produced for these
                    # rejected entries (they are first-write rejections); the frontend
                    # interprets null as "message-level, not bound to a swipe widget".
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
        # The conversation-log write lives only here so it runs exactly once on
        # every exit path — normal completion, an in-loop exception after the
        # message persisted, or cancellation — without the racy "write outside
        # finally, retry inside" pattern that could double-insert the row.
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


def _register_clients(ctx: PipelineContext, client_ref: list[LLMClient] | None, *, include_agent: bool = True) -> None:
    """Expose the turn's LLM client(s) on *client_ref* so a /stop can abort them.

    *include_agent* is False for magic-rewrite, which never spins up the agent.
    """
    if client_ref is None:
        return
    client_ref.append(ctx.client)
    if include_agent and ctx.agent_client:
        client_ref.append(ctx.agent_client)


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
    """Shared tail for every generating entry point: run the pre-pipeline setup,
    the three-pass pipeline, and result consumption, streaming all SSE events
    through. The user row has already been persisted by the caller.

    *pipeline_settings* feeds the setup and the passes (super-regenerate hands a
    rewrite-disabled copy). *consume_settings* is what ``_consume_pipeline``
    persists under; it defaults to *pipeline_settings* and diverges only for
    super-regenerate. *user_message* is the message the writer answers, which is
    not always *last_user_message* (super-regenerate generates from an OOC
    steering message while seeding hooks with the real last user turn).
    *asst_turn_index* is the turn the assistant row is filed at; *log_turn_index*
    is where the conversation-log row lands (see :func:`_conversation_log_writer`).
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
        ctx.director_fragments,
        user_message,
        attachments=attachments,
        phrase_bank=ctx.phrase_bank,
        lorebook_block=setup.lorebook_block,
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
    client_ref: list[LLMClient] | None = None,
) -> AsyncIterator[dict]:
    try:
        if attachments is None:
            attachments = []
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        _register_clients(ctx, client_ref)

        settings = ctx.settings
        messages = await db.get_messages(conversation_id)
        conv = ctx.conv

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        # The user turn this generation answers. For a fresh send it is the
        # turn we mint the user row at (next_turn); for the /continue path
        # (skip_user_persist) the user row already exists, so it is that row's
        # own turn_index. The conversation log is filed at this index so it
        # lands at the user turn in both cases, per _conversation_log_writer.
        user_turn = next_turn

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]
            user_turn = messages[-1]["turn_index"]

        # Derive progressive_fields from the grandparent message node (branch-aware)
        # rather than conversation_logs which are indexed by turn_index and can
        # return data from a different branch after a branch switch.
        grandparent = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        ctx.director["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}

        # Save user message BEFORE pipeline
        if not skip_user_persist:
            # Convert frontend attachment format to database format
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

        # The lorebook scan includes the current user message so its keywords
        # are picked up, not just prior history.
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

    except Exception as e:
        logger.exception("Pipeline error")
        yield {"event": "error", "data": str(e)}


async def handle_fork_edit(
    conversation_id: str,
    user_msg_id: int,
    new_content: str,
    client_ref: list[LLMClient] | None = None,
) -> AsyncIterator[dict]:
    """Fork the conversation at a user message.

    Persists an edited copy of *user_msg_id* as a new sibling (same parent_id
    and turn_index) and generates a fresh reply through the full
    director→writer→editor pipeline -- i.e. a regenerate whose input the user
    rewrote first. The original message and its whole subtree are left intact;
    ``get_messages_with_branch_info`` then reports the user row as a multi-branch
    node and the existing swipe-nav drives navigation between the siblings.

    The user-message persistence mirrors ``handle_turn``; the mood/turn handling
    mirrors ``handle_regenerate`` (moods reset to the branch point, assistant
    logged at the branch turn) because ``conversation_logs.turn_index`` is shared
    across branches -- logging at the assistant turn keeps the new branch's moods
    distinguishable from the original turn's log at the user turn.
    """
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        _register_clients(ctx, client_ref)

        settings = ctx.settings
        original = await db.get_message_by_id(user_msg_id)
        if not original or original["conversation_id"] != conversation_id or original["role"] != "user":
            yield {"event": "error", "data": "Invalid target message"}
            return

        parent_id: int | None = original["parent_id"]
        turn_index = original["turn_index"]
        asst_turn = turn_index + 1
        history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []

        # Reset the director to the pre-turn baseline, exactly like a regenerate:
        # moods as of the branch point, and progressive_fields read branch-aware
        # from the grandparent assistant node (not conversation_logs, which are
        # turn-indexed and can leak across branches).
        ctx.director["active_moods"] = await db.get_moods_before_turn(conversation_id, turn_index)
        grandparent = next((m for m in reversed(history) if m["role"] == "assistant"), None)
        ctx.director["progressive_fields"] = grandparent.get("progressive_fields") or {} if grandparent else {}

        # Carry the original message's user attachments onto the new sibling and
        # into the prompt. DB-format dicts (mime_type/data_b64) pass straight
        # through add_message and build_multimodal_content, as in handle_regenerate.
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

        # Mirrors handle_turn, but logs at the assistant turn (see docstring).
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
            log_turn_index=asst_turn,
        ):
            yield event

    except Exception as e:
        logger.exception("Fork edit error")
        yield {"event": "error", "data": str(e)}


async def handle_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    client_ref: list[LLMClient] | None = None,
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        _register_clients(ctx, client_ref)

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
            lorebook_messages=[*history, {"role": "user", "content": user_msg["content"]}],
            user_message=user_msg["content"],
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=target["turn_index"],
            log_turn_index=target["turn_index"],
        ):
            yield event

    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}


_SUPER_REGEN_MSG = "[OOC: Your response was kind of meh, rewrite it in a slightly different but still realistic direction.]"


async def handle_super_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    client_ref: list[LLMClient] | None = None,
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        _register_clients(ctx, client_ref)

        settings = ctx.settings
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)

        # Extend history to include the original user+assistant exchange so the
        # model sees what it wrote before being asked to go a different direction.
        extended_history = [
            *history,
            {"role": "user", "content": user_msg["content"]},
            {"role": "assistant", "content": target["content"]},
        ]
        # rewrite_user_prompt must not alter the OOC steering message.
        super_regen_settings = {
            **settings,
            "enabled_tools": {
                **(settings.get("enabled_tools") or {}),
                "rewrite_user_prompt": False,
            },
        }

        # Collect audit context from history only — exclude target["content"] so
        # the editor doesn't flag the new draft for repeating the message it replaced.
        editor_audit_msgs = [msg["content"] for msg in reversed(history) if msg.get("role") == "assistant"][:3]

        # The pipeline runs over the extended history with the rewrite-disabled
        # settings and the OOC steering message as the writer's input, while the
        # hooks still see the real last user turn. The result is saved as a
        # sibling of the original (same parent_id / turn_index), and
        # _consume_pipeline persists under the *original* settings — only the
        # pipeline itself needs the rewrite-disabled copy.
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

    except Exception as e:
        logger.exception("Super-regenerate error")
        yield {"event": "error", "data": str(e)}


async def handle_magic_rewrite(
    conversation_id: str,
    assistant_msg_id: int,
    direction: str,
    client_ref: list[LLMClient] | None = None,
) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return

        _register_clients(ctx, client_ref, include_agent=False)

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
        msgs = prefix + [{"role": "user", "content": direction_msg}]

        hyperparams = extract_hyperparams(settings)

        writer_reasoning_on = bool((settings.get("reasoning_enabled_passes") or {}).get("writer", False))
        extra = reasoning_cfg(writer_reasoning_on)

        accumulated = ""
        async for item in ctx.client.complete(messages=msgs, model=settings["model_name"], **extra, **hyperparams):
            if item["type"] == "done":
                break
            if item["type"] == "reasoning":
                # Stream reasoning deltas like the main pipeline does, labelled
                # "writer" since this is a writer-style rewrite with no
                # director/editor passes.
                yield {"event": "reasoning", "data": {"pass": "writer", "delta": item["delta"]}}
            elif item["type"] == "content":
                accumulated += item["delta"]
                yield {"event": "token", "data": item["delta"]}

        # On abort, keep the original message intact rather than overwriting it
        # with the partial rewrite that streamed before the stop.
        if accumulated.strip() and not ctx.client.is_aborted:
            await db.update_message_content(assistant_msg_id, accumulated)

        # Emitted on the success path only — on error we yield "error" and stop,
        # matching the other entry points (whose "done" lives in _consume_pipeline
        # and is skipped when an exception aborts the turn).
        yield {"event": "done"}

    except Exception as e:
        logger.exception("Magic rewrite error")
        yield {"event": "error", "data": str(e)}
