"""
passes/director.py — Director pass: the pre-processing phase that selects
moods, plot direction, and optionally rewrites the user's prompt before
the writer pass runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Optional, Sequence

from ...cached_call import CachedBase
from ...kv_tracker import _KVCacheTracker
from ...llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ...llm_types import ChatMessage
from ...prompt_builder import (
    build_director_tool_prompt,
    compute_agentic_lorebook_block,
    compute_style_injection_block,
)
from ...tool_registry import (
    PRE_WRITER_TOOLS,
    TOOLS,
    build_direct_scene_tool,
)
from ...utils import build_multimodal_content, extract_hyperparams
from .prompt_rewrite import (
    apply_rewrite,
    extract_rewritten_message,
    order_director_tools,
    suppresses_reasoning,
)

if TYPE_CHECKING:
    from ...macros import Macros
    from ...pipeline_state import TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


# ── Agentic-lorebook gating + tool override ───────────────────────────────────


def _agentic_lorebook_active(
    settings: Mapping[str, Any],
    enabled_tools: Mapping[str, bool],
    lorebook_entries: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Whether the Director drives lorebook activation this turn.

    True only when the feature flag is on, the agent + ``direct_scene`` are on,
    and at least one *non-constant* candidate entry exists to offer in the
    catalog. Constant entries are always injected and never managed by the
    Director, so a pool of only constants does not enable agentic mode. Both
    tools-blob call sites (the main turn and magic-rewrite) read the same
    settings/entries through this helper, so the cached blob stays consistent.

    *agent_on* is passed in (rather than recomputed) so the orchestrator's
    ``agent_enabled`` stays the single source of truth — mirroring
    ``resolve_length_guard``.
    """
    if not bool(settings.get("agentic_lorebook_enabled", 0)):
        return False
    if not agent_on:
        return False
    if not bool(enabled_tools.get("direct_scene", False)):
        return False
    return any(not e.get("constant") for e in lorebook_entries)


def build_direct_scene_override(
    writer_fragments: Sequence[Mapping[str, Any]],
    *,
    agentic_lorebook: bool,
) -> dict:
    """Build the ``direct_scene`` dynamic-tool schema from *writer_fragments*.

    Thin wrapper over :func:`~backend.tool_registry.build_direct_scene_tool` so the
    orchestrator's tools-blob composition (``_build_writer_tools_blob``) reaches
    the direct_scene schema through the director module rather than importing the
    schema builder directly — symmetric to ``build_feedback_override``.
    """
    return build_direct_scene_tool(writer_fragments, agentic_lorebook=agentic_lorebook)


@dataclass
class DirectorResult:
    """Typed payload of the director pass's terminal ``done`` event.

    Field names match the orchestrator's turn-state locals and
    :class:`~backend.orchestrator._PipelineResult` (notably ``agent_raw``,
    ``rewritten_msg``) so a single name follows each value end to end. This
    replaces the former 6-positional ``result`` tuple: adding or reordering a
    field can no longer silently transpose values at the unpack site.

    ``progressive_fields`` is intentionally absent — it is derived in the
    orchestrator from ``extra_fields`` filtered against the valid progressive
    fragment ids, which the director pass does not know about.
    """

    active_moods: list[str] = field(default_factory=list)
    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    rewritten_msg: str | None = None
    extra_fields: dict = field(default_factory=dict)
    selected_lorebook_entries: list[str] = field(default_factory=list)


# ── Tool-call result unpacking ────────────────────────────────────────────────


def apply_tool_calls(
    tool_calls: list[dict],
    current_moods: list[str],
) -> tuple[list[str], str | None, dict, list[str]]:
    """Extract values from tool calls.

    Returns (moods, refined_message, extra_fields, selected_lorebook_entries).
    extra_fields contains all direct_scene args except moods and selected_lorebook_entries.
    selected_lorebook_entries is the Director's agentic lorebook selection (entry names);
    it is pulled out explicitly so it never renders as a Scene Direction field.
    """
    moods = list(current_moods)
    refined: str | None = None
    extra_fields: dict = {}
    selected_lorebook_entries: list[str] = []

    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods = args.get("moods", [])
            al = args.get("selected_lorebook_entries")
            selected_lorebook_entries = [str(x) for x in al] if isinstance(al, list) else []
            extra_fields = {
                k: v for k, v in args.items() if k not in ("moods", "selected_lorebook_entries") and v not in (None, "", [])
            }
        elif tc["name"] == "rewrite_user_prompt":
            refined = extract_rewritten_message(args)

    return (moods, refined, extra_fields, selected_lorebook_entries)


# ── Agent pass ────────────────────────────────────────────────────────────────


async def director_pass(
    client: LLMClient,
    base: CachedBase,
    user_message: str,
    settings: Mapping[str, Any],
    director: Mapping[str, Any],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    enabled_tools: Mapping[str, bool],
    attachments: Optional[Sequence[Mapping[str, Any]]] = None,
    kv_tracker=None,
    reasoning_on: bool = True,
    lorebook_block: str = "",
    lorebook_catalog: str = "",
    progressive_state: dict | None = None,
) -> AsyncIterator[dict]:
    """Yields reasoning dicts during each tool call, then a single done dict.

    Yields:
        {"type": "reasoning", "delta": str}        — zero or more reasoning chunks
        {"type": "done", "result": DirectorResult}  — terminal pass result
    """
    active_moods = director["active_moods"]
    if attachments is None:
        attachments = []

    refined_msg: str | None = None
    extra_fields: dict = {}
    selected_lorebook_entries: list[str] = []
    all_calls: list[dict] = []
    last_raw = ""

    tool_names = [n for n, on in enabled_tools.items() if on and n in PRE_WRITER_TOOLS]

    # Enforce priority order: rewrite_user_prompt first so users can abort
    # early if they dislike the rewrite before the full director runs.
    if len(tool_names) > 1:
        tool_names = order_director_tools(tool_names)

    if not tool_names:
        yield {
            "type": "done",
            "result": DirectorResult(active_moods=active_moods),
        }
        return

    # The tools blob is resolved once into the shared base; the director reads it
    # rather than rebuilding it, so it cannot drift from the writer/editor blobs.
    tool_schemas = list(base.tools)

    logger.info(
        "Director pass: tools included=%s",
        (json.dumps([s["function"]["name"] for s in tool_schemas]) if tool_schemas else "[]"),
    )

    t0 = time.monotonic()
    for name in tool_names:
        if client.is_aborted:
            break
        tool_schema = next((s for s in tool_schemas if s["function"]["name"] == name), None)
        tool_tail = build_director_tool_prompt(
            name,
            user_message,
            active_moods,
            mood_fragments,
            reasoning_on=reasoning_on,
            interactive_fragments=interactive_fragments,
            progressive_state=progressive_state,
            tool_schema=tool_schema,
            lorebook_catalog=lorebook_catalog,
        )
        tail = ("___\n\n" + lorebook_block + "\n\n" if lorebook_block else "") + tool_tail
        content = build_multimodal_content(tail, attachments)
        trailing: list[ChatMessage] = [{"role": "user", "content": content}]
        logger.info(
            "Agent tool=%s prompt:\n%s",
            name,
            json.dumps([*base.prefix, *trailing], indent=2, ensure_ascii=False),
        )
        resp: dict = {}
        # Errors are not caught here: a failed tool call propagates out of the
        # pass and aborts the turn, like the writer/editor passes.
        reasoning_params = reasoning_cfg(reasoning_on and not suppresses_reasoning(name))
        hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
        async for event in base.complete(
            client,
            label=f"director:{name}",
            trailing=trailing,
            tool_choice=TOOLS[name]["choice"],
            kv_tracker=kv_tracker,
            **hyperparams,
            **reasoning_params,
        ):
            if event["type"] == "reasoning":
                yield {"type": "reasoning", "delta": event["delta"]}
            elif event["type"] == "done":
                resp = event["message"]
        last_raw = json.dumps(resp, default=str)
        logger.info("Agent tool=%s output:\n%s", name, last_raw)
        if parsed := parse_tool_calls(resp):
            all_calls.extend(parsed)
            active_moods, new_refined, new_extra, new_lorebook = apply_tool_calls(parsed, active_moods)
            if new_refined:
                refined_msg = new_refined
            if new_extra:
                extra_fields.update(new_extra)
            if new_lorebook:
                selected_lorebook_entries = new_lorebook
        else:
            logger.info("Agent tool=%s: model skipped", name)

    yield {
        "type": "done",
        "result": DirectorResult(
            active_moods=active_moods,
            agent_raw=last_raw,
            calls=all_calls,
            latency=int((time.monotonic() - t0) * 1000),
            rewritten_msg=refined_msg,
            extra_fields=extra_fields,
            selected_lorebook_entries=selected_lorebook_entries,
        ),
    }


async def director_stage(
    cfg: "_PipelineConfig",
    state: "TurnState",
    *,
    settings: Mapping[str, Any],
    director: Mapping[str, Any],
    mood_fragments: Sequence[Mapping[str, Any]],
    writer_fragments: Sequence[Mapping[str, Any]],
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: _KVCacheTracker,
    lorebook_block: str,
    lorebook_catalog: str,
    lorebook_entries: Sequence[Mapping[str, Any]] | None,
    lorebook_messages: Sequence[Mapping[str, Any]] | None,
    agentic_lorebook: bool,
    macros: "Macros",
) -> AsyncIterator[dict]:
    """Director stage: input-prep + director pass + all of its output
    post-processing, owned here so the orchestrator only sequences passes.

    Runs the director pass (when the agent is on and a pre-writer tool is
    enabled), folding its :class:`DirectorResult` into *state*; applies the
    optional prompt rewrite; computes the style-injection block (→
    ``director_done``); and computes the writer's lorebook block (agentic
    selection vs. the keyword-scanned block). Returns early on a director-phase
    abort so ``director_done`` and the style/lorebook work are skipped — the
    orchestrator's own post-stage abort check then halts before the writer (the
    writer and agent clients share one abort token).
    """
    # --- Director pass ---
    has_pre_writer_tools = any(cfg.enabled_tools.get(n, False) for n in PRE_WRITER_TOOLS)
    if cfg.agent_on and has_pre_writer_tools:
        yield {"event": "director_start"}
        async for event in director_pass(
            cfg.agent_lane.client,
            cfg.agent_lane.base,
            state.user_message,
            settings,
            director,
            mood_fragments,
            writer_fragments,
            cfg.enabled_tools,
            attachments=attachments,
            kv_tracker=kv_tracker,
            reasoning_on=cfg.director_reasoning_on,
            lorebook_block=lorebook_block,
            lorebook_catalog=lorebook_catalog,
            progressive_state=state.progressive_state,
        ):
            if event["type"] == "reasoning":
                state.reasoning_director += event["delta"]
                yield {
                    "event": "reasoning",
                    "data": {"pass": "director", "delta": event["delta"]},
                }
            elif event["type"] == "done":
                result: DirectorResult = event["result"]
                state.active_moods = result.active_moods
                state.agent_raw = result.agent_raw
                state.calls = result.calls
                state.latency = result.latency
                state.rewritten_msg = result.rewritten_msg
                state.extra_fields = result.extra_fields
                state.selected_lorebook_entries = result.selected_lorebook_entries
                state.progressive_fields = {k: v for k, v in state.extra_fields.items() if k in state.valid_progressive_ids}
        state.effective_msg, did_rewrite = apply_rewrite(state.user_message, state.rewritten_msg)
        if did_rewrite:
            yield {"event": "prompt_rewritten", "data": {"refined_message": state.rewritten_msg}}

    # Bail out if stop was clicked during the director pass: skip style injection,
    # director_done, and the writer-lorebook computation, exactly as before. The
    # orchestrator's own post-stage abort check then halts the pipeline before the
    # writer. The writer and agent clients share one abort token, so checking
    # either is equivalent.
    if cfg.agent_lane.client.is_aborted:
        return

    # Style injection
    direct_scene_enabled = cfg.agent_on and bool(cfg.enabled_tools.get("direct_scene", False))
    state.inj_block = macros.resolve_message(
        compute_style_injection_block(
            state.active_moods,
            director["active_moods"],
            mood_fragments,
            writer_fragments,
            direct_scene_enabled,
            state.extra_fields,
            state.progressive_state,
        )
    )

    yield {
        "event": "director_done",
        "data": {
            "active_moods": state.active_moods,
            "injection_block": state.inj_block,
            "tool_calls": state.calls,
            "agent_latency_ms": state.latency,
            "extra_fields": state.extra_fields,
        },
    }

    # In agentic mode the writer's lorebook block is computed *after* the director
    # pass, from its selection (constants ∪ Director-named entries) — the keyword
    # scan was bypassed up front (lorebook_block is ""), and the catalog only fed
    # the director. Otherwise the keyword-scanned lorebook_block is used as-is.
    if agentic_lorebook:
        state.writer_lorebook_block = compute_agentic_lorebook_block(
            lorebook_entries or [], state.selected_lorebook_entries, macros, lorebook_messages
        )
    else:
        state.writer_lorebook_block = lorebook_block
