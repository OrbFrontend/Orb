"""
workflow_bridge.py — The single point where the pipeline talks to workflows.

Iterates PRE_PIPELINE and POST_PIPELINE hook subscriptions, validates every
event a hook yields (tool enables, system-prompt blocks, draft replacements,
attachment artifacts, per-message state), and rejects malformed or
underscore-prefixed events so one bad hook can neither crash a turn nor
impersonate an internal event.

Both entry points take the turn's captured ``RegistrySnapshot`` as a required
argument rather than reading the global registry pointer themselves. That is
the whole point of the snapshot: a turn resolves its hook set once, so an
install landing between the pre- and post-hook phases cannot pair an old
pre-hook with a new post-hook.

Depends only downward (``workflows``, ``inference``, ``core``); imports no
pipeline sibling, so both the pre-pipeline setup path and the post-pipeline
orchestrator path can safely import it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

from ..core import ChatMessage, workflow_character_state_lock, workflow_state_lock
from ..inference import TOOLS, LLMClient, _KVCacheTracker
from ..workflows import (
    EV_ATTACH_ARTIFACT,
    EV_CONTEXT_BLOCK,
    EV_DRAFT_REPLACED,
    EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE,
    EV_SYSTEM_PROMPT,
    HookStage,
    HookType,
    PostCtx,
    PreCtx,
    RegistrySnapshot,
    Subscription,
    WorkflowSource,
    _readonly,
    public_event_error,
)
from ..workflows.enablement import effective_workflow_enabled

logger = logging.getLogger(__name__)

MAX_CONTEXT_BLOCK_BYTES = 8 * 1024
MAX_CONTEXT_BYTES_PER_TARGET = 32 * 1024
"""Per-block and per-target-per-turn budgets for extension context blocks.

Restated here rather than imported from ``features/extensions`` because the
pipeline sits *below* that slice in the layer stack and must not import it. The
interpreter enforces the per-block bound at the moment a flow stages one; this
module enforces the combined bound, which only the pipeline can see because
only it knows every extension contributing to the same turn.
"""


@asynccontextmanager
async def _hook_locks(sub: Subscription, conversation_id: str | None, character_id: str | None):
    """Hold the workflow state locks for a trusted hook; hold nothing for a community one.

    Trusted Python hooks read and write their state slots through the toolkit at
    arbitrary points inside the callable, so the bridge has to serialize their
    whole lifetime. A community adapter derives its own lock plan from the
    compiled flow and acquires exactly the scopes that flow touches -- and
    ``asyncio.Lock`` is not reentrant, so taking the same lock around the
    adapter would deadlock it against itself the moment it read its own state.
    """
    if sub.source is WorkflowSource.COMMUNITY:
        yield
        return
    async with (
        workflow_state_lock(conversation_id or "", sub.workflow_id),
        workflow_character_state_lock(character_id or "", sub.workflow_id),
    ):
        yield


def _public_hook_event(ev: object, *, hook_type: str, workflow_id: str) -> dict | None:
    """Return a valid public SSE event, or log and drop malformed output.

    Control events are consumed before this boundary. Anything left must use
    the public ``{"event": <non-empty str>, ...}`` shape; accepting arbitrary
    objects here merely defers the failure to the SSE adapter. Shape validation
    lives in ``workflows.contracts.public_event_error`` so this bridge and the
    API on-demand SSE encoder enforce one definition of a public event.
    """
    reason = public_event_error(ev)
    if reason is not None:
        logger.warning(
            "%s hook %r yielded an invalid public event (%s); dropping",
            hook_type,
            workflow_id,
            reason,
        )
        return None
    return cast(dict, ev)


@dataclass(slots=True)
class _PostPipelineResult:
    """Final value of :func:`_run_post_pipeline`: the (possibly rewritten) draft
    plus any attachments and per-message state staged for persistence."""

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
    registry: RegistrySnapshot,
) -> AsyncIterator[dict | _PostPipelineResult]:
    """Run every POST_PIPELINE workflow hook over the finished draft.

    Streams pass-through SSE events and yields one final
    :class:`_PostPipelineResult` when all hooks have run. Hook failures are
    logged and skipped so one bad hook cannot crash the turn -- and a failure
    discards only that hook's invocation, never an earlier hook's successful
    transform.

    Execution runs in two phases, in the order *registry* resolved:
    ``transform`` subscriptions (each feeding its output to the next), then
    ``observe`` subscriptions (all seeing the same final draft). An observer's
    ``draft_replaced`` is dropped rather than applied; a hook does not get to
    decide its own stage at runtime, because the ordering guarantee the other
    hooks rely on was fixed when the snapshot was resolved.
    """
    staged_attachments: list[dict] = []
    staged_message_state: dict[str, dict] = {}
    for sub in registry.subscriptions(HookType.POST_PIPELINE):
        if not effective_workflow_enabled(sub.workflow_id, settings):
            logger.info("workflow %r post-pipeline hook suspended (disabled)", sub.workflow_id)
            continue
        replaced_this_hook = False
        # Serialize same-(cid, workflow_id) writers against concurrent
        # /trigger calls and any other in-flight pipeline that reaches this
        # hook on the same conversation. Different workflows on the same
        # conversation keep distinct lock keys, so they still run in parallel.
        # A community adapter is exempt and locks itself -- see _hook_locks.
        async with _hook_locks(sub, conversation_id, character_id):
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
                    runtime_generation=registry.generation,
                )
                async for ev in sub.callable(post_ctx):
                    t = ev.get("type") if isinstance(ev, dict) else None
                    if t == EV_DRAFT_REPLACED:
                        if sub.stage is HookStage.OBSERVE:
                            logger.warning(
                                "post_pipeline hook %r yielded draft_replaced from the observe stage; ignoring "
                                "(an observer sees the final draft and cannot rewrite it)",
                                sub.workflow_id,
                            )
                            continue
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
                    if t == EV_ATTACH_ARTIFACT:
                        # Only workflows with produces_artifacts=True may persist attachments.
                        w = registry.get(sub.workflow_id)
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
                    if t == EV_SET_MESSAGE_STATE:
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
                    # A dict carrying a "type" key is a control event; if it matched
                    # no known branch above it is malformed (e.g. a typo'd type, or a
                    # leaked sub-generator terminal). Drop it rather than letting it
                    # fall through and be emitted to the client as a stray SSE event.
                    if t is not None:
                        logger.warning(
                            "post_pipeline hook %r yielded unknown control event type %r; dropping",
                            sub.workflow_id,
                            t,
                        )
                        continue
                    public_event = _public_hook_event(
                        ev,
                        hook_type="post_pipeline",
                        workflow_id=sub.workflow_id,
                    )
                    if public_event is not None:
                        yield public_event
            except Exception:
                logger.exception("post_pipeline hook %r failed", sub.workflow_id)

    yield _PostPipelineResult(draft, staged_attachments, staged_message_state)


def _stage_workflow_attachment(att: object, workflow_id: str) -> dict | None:
    """Validate and normalize a workflow ``attach_artifact`` entry.

    Returns a bytes-only dict ready for ``add_message``, or ``None`` if
    validation fails (logged as a warning). Never raises — bad workflow output
    must not crash the turn.
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


def _context_blocks_error(blocks: list[dict[str, Any]], collected: list[dict]) -> str | None:
    """Validate a candidate invocation's complete context effect set.

    Community adapters call this before committing their other effects. The
    bridge calls the same function defensively when consuming each emitted
    block, so the pre-commit and publication boundaries cannot drift.
    """
    pending = list(collected)
    for block in blocks:
        targets = block.get("targets")
        label = block.get("label")
        text = block.get("text")
        if not isinstance(text, str) or not text.strip() or not isinstance(label, str):
            return "context block is malformed"
        if (
            not isinstance(targets, (list, tuple))
            or not targets
            or any(target not in ("director", "writer") for target in targets)
        ):
            return "context block has invalid targets"
        size = len(text.encode("utf-8"))
        if size > MAX_CONTEXT_BLOCK_BYTES:
            return f"context block exceeds the {MAX_CONTEXT_BLOCK_BYTES} byte limit"
        for target in targets:
            used = sum(len(item["text"].encode("utf-8")) for item in pending if target in item["targets"])
            if used + size > MAX_CONTEXT_BYTES_PER_TARGET:
                return f"context blocks exceed the {MAX_CONTEXT_BYTES_PER_TARGET} byte {target} turn budget"
        pending.append({"targets": list(targets), "label": label, "text": text})
    return None


def _accumulate_context_block(ev: Mapping[str, Any], workflow_id: str, collected: list[dict]) -> None:
    """Validate one ``context_block`` yield and fold it into the turn's collection."""
    candidate = dict(ev)
    reason = _context_blocks_error([candidate], collected)
    if reason is not None:
        logger.warning("pre_pipeline hook %r yielded a rejected context_block: %s", workflow_id, reason)
        return
    collected.append(
        {
            "extension_id": workflow_id,
            "targets": list(candidate["targets"]),
            "label": candidate["label"],
            "text": candidate["text"],
        }
    )


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
    registry: RegistrySnapshot,
) -> AsyncIterator[dict]:
    """Run every PRE_PIPELINE workflow hook before the pipeline starts.

    Yields pass-through SSE events and mutates *accumulators* in place:
    ``enable_tools`` yields fold extra tools into the merged map;
    ``system_prompt`` yields append blocks to the extras list; ``context_block``
    yields append to the per-turn trailing-context collection. Hook failures
    are logged and skipped.

    *accumulators* must be pre-populated with
    ``{"merged_enabled_tools": <dict>, "extras": [], "context_blocks": []}``.
    """
    for sub in registry.subscriptions(HookType.PRE_PIPELINE):
        if not effective_workflow_enabled(sub.workflow_id, settings):
            logger.info("workflow %r pre-pipeline hook suspended (disabled)", sub.workflow_id)
            continue
        # Lock held for the hook's full lifetime to keep workflow_state RMW atomic.
        async with _hook_locks(sub, conversation_id, character_id):
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
                    runtime_generation=registry.generation,
                    context_block_error=lambda blocks: _context_blocks_error(blocks, accumulators["context_blocks"]),
                )
                async for ev in sub.callable(pre_ctx):
                    t = ev.get("type") if isinstance(ev, dict) else None
                    if t == EV_ENABLE_TOOLS:
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
                    if t == EV_SYSTEM_PROMPT:
                        block = ev.get("block")
                        if not isinstance(block, str) or not block.strip():
                            logger.warning(
                                "pre_pipeline hook %r yielded empty/whitespace-only system_prompt; ignoring",
                                sub.workflow_id,
                            )
                            continue
                        accumulators["extras"].append(block)
                        continue
                    if t == EV_CONTEXT_BLOCK:
                        _accumulate_context_block(ev, sub.workflow_id, accumulators["context_blocks"])
                        continue
                    # Unknown control event ("type" present but unmatched): drop it
                    # instead of leaking it through as a stray SSE event.
                    if t is not None:
                        logger.warning(
                            "pre_pipeline hook %r yielded unknown control event type %r; dropping",
                            sub.workflow_id,
                            t,
                        )
                        continue
                    public_event = _public_hook_event(
                        ev,
                        hook_type="pre_pipeline",
                        workflow_id=sub.workflow_id,
                    )
                    if public_event is not None:
                        yield public_event
            except Exception:
                logger.exception("pre_pipeline hook %r failed", sub.workflow_id)
