"""
passes/director/director.py — The director pass: selects moods and plot direction,
and optionally rewrites the user's prompt before the writer runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Mapping, Optional, Sequence

from ....core import ChatMessage, build_multimodal_content, extract_hyperparams
from ....inference import (
    PRE_WRITER_TOOLS,
    TOOLS,
    CachedBase,
    LLMClient,
    _KVCacheTracker,
    build_direct_scene_tool,
    build_director_scene_step_prompt,
    build_director_tool_prompt,
    compute_style_injection_block,
    parse_tool_calls,
    reasoning_cfg,
    render_direction_notes_block,
)
from ...predicates import direction_note_to_director, direction_note_to_writer
from . import progressive
from .lorebook_select import LorebookSelectResult, lorebook_select_step
from .prompt_rewrite import (
    apply_rewrite,
    extract_rewritten_message,
    order_director_tools,
    suppresses_reasoning,
)

if TYPE_CHECKING:
    from ....core import Macros
    from ...state import LorebookTurn, TurnState, _PipelineConfig

logger = logging.getLogger(__name__)


# ── direct_scene tool override ────────────────────────────────────────────────


def build_direct_scene_override(
    writer_fragments: Sequence[Mapping[str, Any]],
) -> dict:
    """Build the ``direct_scene`` tool schema from *writer_fragments*.

    Thin wrapper over ``build_direct_scene_tool`` so ``_build_writer_tools_blob``
    reaches the schema through the director module rather than importing the
    schema builder directly — symmetric to ``build_feedback_override``.
    """
    return build_direct_scene_tool(writer_fragments)


def _step_schema(tool_schema: dict, keep: str) -> dict | None:
    """Single-field variant of the ``direct_scene`` parameters for one step call.

    Passed as the per-call ``json_schema`` decoding constraint so the model
    physically cannot fill any field but the step's target — text mode applies
    it to the grammar (prompt bytes and KV cache untouched); the chat transport
    drops it and relies on the post-parse filter in the loop below.
    """
    params = tool_schema["function"]["parameters"]
    prop = params.get("properties", {}).get(keep)
    if prop is None:
        return None
    return {
        "type": "object",
        "properties": {keep: prop},
        "required": [keep] if keep in (params.get("required") or []) else [],
    }


@dataclass
class DirectorResult:
    """Typed result of the director pass, yielded as the ``done`` event payload.

    Field names match ``TurnState`` (e.g. ``agent_raw``, ``rewritten_msg``) so
    the same name follows each value from the pass through to persistence.

    ``progressive_fields`` is absent — it is derived in ``director_stage`` by
    filtering ``extra_fields`` through ``progressive.select``.
    """

    active_moods: list[str] = field(default_factory=list)
    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    rewritten_msg: str | None = None
    extra_fields: dict = field(default_factory=dict)


# ── Tool-call result unpacking ────────────────────────────────────────────────


def apply_tool_calls(
    tool_calls: list[dict],
    current_moods: list[str],
) -> tuple[list[str], str | None, dict]:
    """Extract values from tool calls.

    Returns ``(moods, refined_message, extra_fields)``. ``extra_fields`` holds all
    ``direct_scene`` args except moods. (Lorebook selection is handled separately
    by the ``select_lorebook`` step, not this tool.)
    """
    moods = list(current_moods)
    refined: str | None = None
    extra_fields: dict = {}

    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods = args.get("moods", [])
            extra_fields = {k: v for k, v in args.items() if k != "moods" and v not in (None, "", [])}
        elif tc["name"] == "rewrite_user_prompt":
            refined = extract_rewritten_message(args)

    return (moods, refined, extra_fields)


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
    progressive_state: dict | None = None,
    direction_notes_block: str = "",
) -> AsyncIterator[dict]:
    """Yield reasoning chunks during each tool call, then a single done dict.

    Yields:
        ``{"type": "reasoning", "delta": str}``       — zero or more reasoning chunks
        ``{"type": "done", "result": DirectorResult}`` — terminal pass result
    """
    active_moods = director["active_moods"]
    if attachments is None:
        attachments = []

    refined_msg: str | None = None
    extra_fields: dict = {}
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

    per_fragment_on = bool(settings.get("director_individual_fragments", 0))

    # Prepended to direct_scene prompts as "___"-fenced sections (like the lorebook),
    # so the director decides the scene with the world facts and the established
    # direction notes in view. Notes go only into direct_scene, never rewrite_user_prompt.
    lorebook_prefix = ("___\n\n" + lorebook_block + "\n\n") if lorebook_block else ""
    notes_prefix = ("___\n\n" + direction_notes_block + "\n\n") if direction_notes_block else ""

    t0 = time.monotonic()
    for name in tool_names:
        if client.is_aborted:
            break
        tool_schema = next((s for s in tool_schemas if s["function"]["name"] == name), None)
        if name == "direct_scene" and per_fragment_on and interactive_fragments:
            reasoning_params = reasoning_cfg(reasoning_on and not suppresses_reasoning(name))
            hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})

            # One forced call per fragment, each shown the values already chosen
            # this turn so later fragments build on earlier ones. Moods are
            # resolved first, in a call of their own.
            decided: list[tuple[str, Any]] = []
            for stage in [None, *interactive_fragments]:
                if client.is_aborted:
                    break
                target = stage["id"] if stage else "moods"
                step_tail = build_director_scene_step_prompt(
                    user_message,
                    active_moods,
                    mood_fragments,
                    tool_schema=tool_schema,
                    reasoning_on=reasoning_on,
                    target_fragment=stage,
                    decided_fields=decided,
                    progressive_prior=(progressive_state or {}).get(stage["id"]) if stage else None,
                )
                step_tail = lorebook_prefix + notes_prefix + step_tail
                content = build_multimodal_content(step_tail, attachments)
                trailing = [{"role": "user", "content": content}]
                logger.info(
                    "Agent tool=direct_scene target=%s prompt:\n%s",
                    target,
                    json.dumps([*base.prefix, *trailing], indent=2, ensure_ascii=False),
                )
                resp = {}
                try:
                    async for event in base.complete(
                        client,
                        label="director:direct_scene",
                        trailing=trailing,
                        tool_choice=TOOLS["direct_scene"]["choice"],
                        kv_tracker=kv_tracker,
                        json_schema=_step_schema(tool_schema, target) if tool_schema else None,
                        **hyperparams,
                        **reasoning_params,
                    ):
                        if event["type"] == "reasoning":
                            yield {"type": "reasoning", "delta": event["delta"]}
                        elif event["type"] == "done":
                            resp = event["message"]
                except Exception:
                    # A failed call skips this fragment but must not propagate: the
                    # remaining fragments and the writer still run, like the
                    # lorebook-select and direction-note steps. Aborting the turn
                    # here would also skip persisting the finished reply.
                    logger.exception("Agent tool=direct_scene target=%s: call failed; skipping", target)
                    continue
                last_raw = json.dumps(resp, default=str)
                logger.info("Agent tool=direct_scene target=%s output:\n%s", target, last_raw)
                parsed = parse_tool_calls(resp)
                if not parsed:
                    logger.info("Agent tool=direct_scene target=%s: model skipped", target)
                    continue
                # The model often fills fields besides the step's target despite the
                # "Fill ONLY" instruction (the byte-stable schema still offers them
                # all). Only the target is kept below, so strip the extras from the
                # recorded call too — otherwise the inspector/tool-call log shows
                # every step re-deciding fragments already settled earlier.
                keep = "moods" if stage is None else stage["id"]
                for tc in parsed:
                    if tc.get("name") == "direct_scene":
                        tc["arguments"] = {k: v for k, v in tc.get("arguments", {}).items() if k == keep}
                all_calls.extend(parsed)
                if stage is None:
                    # Reuse the shared unpacker so moods behave exactly as in the
                    # combined call; any fragment values it returns are dropped,
                    # each fragment being produced in its own call.
                    active_moods, _, _ = apply_tool_calls(parsed, active_moods)
                else:
                    args = next((tc.get("arguments", {}) for tc in parsed if tc.get("name") == "direct_scene"), {})
                    val = args.get(stage["id"])
                    if val not in (None, "", []):
                        extra_fields[stage["id"]] = val
                    decided.append((stage["injection_label"], val))
            continue
        tool_tail = build_director_tool_prompt(
            name,
            user_message,
            active_moods,
            mood_fragments,
            reasoning_on=reasoning_on,
            interactive_fragments=interactive_fragments,
            progressive_state=progressive_state,
            tool_schema=tool_schema,
        )
        tail = lorebook_prefix + (notes_prefix if name == "direct_scene" else "") + tool_tail
        content = build_multimodal_content(tail, attachments)
        trailing: list[ChatMessage] = [{"role": "user", "content": content}]
        logger.info(
            "Agent tool=%s prompt:\n%s",
            name,
            json.dumps([*base.prefix, *trailing], indent=2, ensure_ascii=False),
        )
        resp: dict = {}
        # A failed call skips this tool but must not propagate: the remaining
        # tools and the writer still run, like the lorebook-select and
        # direction-note steps. Aborting the turn here would also skip
        # persisting the finished reply.
        reasoning_params = reasoning_cfg(reasoning_on and not suppresses_reasoning(name))
        hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
        try:
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
        except Exception:
            logger.exception("Agent tool=%s: call failed; skipping", name)
            continue
        last_raw = json.dumps(resp, default=str)
        logger.info("Agent tool=%s output:\n%s", name, last_raw)
        if parsed := parse_tool_calls(resp):
            all_calls.extend(parsed)
            active_moods, new_refined, new_extra = apply_tool_calls(parsed, active_moods)
            if new_refined:
                refined_msg = new_refined
            if new_extra:
                extra_fields.update(new_extra)
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
    lorebook: "LorebookTurn",
    macros: "Macros",
) -> AsyncIterator[dict]:
    """Input-prep + director pass + all post-processing for the director stage.

    Runs the director pass (when the agent is on and a pre-writer tool is
    enabled), folds the :class:`DirectorResult` into *state*, applies the
    optional prompt rewrite, computes the style-injection block (→
    ``director_done``), and computes the writer's lorebook block (agentic
    selection or keyword scan). Returns early on a stop during the director pass
    so ``director_done`` and lorebook work are skipped.
    """
    # Prior progressive state: the seed for this turn, filtered to the fragments
    # currently marked progressive. Used to feed the director pass and (as prior
    # state) the style-injection block — the symmetric counterpart of the output
    # filter below.
    prior_progressive = progressive.select(director.get("progressive_fields", {}), writer_fragments)

    # Render the stored direction notes once; the director receives them in its
    # direct_scene prompt when it is a chosen recipient (so it steers consistent with
    # the direction it set earlier), the writer in its Scene Direction when it is (below).
    direction_notes = director.get("direction_notes") or []
    notes_block = macros.resolve_message(render_direction_notes_block(direction_notes)) if direction_notes else ""

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
            lorebook_block=lorebook.block,
            progressive_state=prior_progressive,
            direction_notes_block=notes_block if direction_note_to_director(settings) else "",
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
                state.progressive_fields = progressive.select(state.extra_fields, writer_fragments)
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

    # --- Agentic lorebook selection ---
    # Its own forced select_lorebook call, independent of direct_scene, so agentic
    # lorebook works whether or not the Director's scene-direction tool is enabled.
    # Runs before director_done so its picks ride state.calls into the inspector/log.
    if lorebook.agentic:
        async for event in lorebook_select_step(
            cfg.agent_lane.client,
            cfg.agent_lane.base,
            settings=settings,
            catalog=lorebook.catalog,
            user_message=state.user_message,
            kv_tracker=kv_tracker,
            reasoning_on=cfg.director_reasoning_on,
        ):
            if event["type"] == "reasoning":
                state.reasoning_director += event["delta"]
                yield {"event": "reasoning", "data": {"pass": "director", "delta": event["delta"]}}
            elif event["type"] == "done":
                sel: LorebookSelectResult = event["result"]
                state.selected_lorebook_entries = sel.selected
                # Append to the turn's calls so the picks stay visible in the
                # conversation log / inspector (they used to ride direct_scene's args).
                state.calls = [*state.calls, *sel.calls]

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
            prior_progressive,
        )
    )
    # Direction notes ride the writer's Scene Direction when the writer is a chosen
    # recipient -- independent of direct_scene and of whether recording is on -- so they
    # are appended here rather than routed through compute_style_injection_block (which
    # clears its inputs when direct_scene is off). scene_direction keeps the pre-append
    # text for the pre-writer notes step.
    state.scene_direction = state.inj_block
    if notes_block and direction_note_to_writer(settings):
        state.inj_block = (state.inj_block + "\n\n" + notes_block).strip()

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

    # The writer's lorebook block, computed once from the per-turn bundle. In
    # substring mode this reuses the keyword-scanned block already built up front;
    # in agentic mode it is the union of constants, the current-turn keyword scan,
    # and the Director's selection (computed now that the selection is known).
    state.writer_lorebook_block = lorebook.writer_block(state.selected_lorebook_entries, macros)
