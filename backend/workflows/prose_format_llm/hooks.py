"""Pipeline and HTTP bindings for the prose-format workflow.

PRE runs the single automatic analysis attempt; POST runs the judge/enforce
loop over the finished draft; ON_DEMAND is the menu RPC. State reads/writes use
the toolkit helpers directly: the framework already holds
``workflow_state_lock`` for the full lifetime of each hook, so they must not
re-acquire it.
"""

from __future__ import annotations

from ..contracts import EV_DRAFT_REPLACED
from ..toolkit import get_workflow_config, get_workflow_state, set_workflow_state
from . import WORKFLOW_ID
from .loop import make_enforce_fn, make_judge_fn, run_analyzer, run_enforcement_loop
from .patching import apply_patches
from .statedoc import filled_elements, is_armed, seed


def _as_n(value) -> int:
    """Coerce max_iterations to a non-negative int, falling back to the default on
    a malformed config slot rather than raising on the per-turn path."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 1


def _str_map(d: dict) -> dict[str, str]:
    """Keep only string-valued entries -- a non-string value would later crash the
    state read that computes ``armed`` (mirrors the analyzer's own guard)."""
    return {str(k): v for k, v in d.items() if isinstance(v, str)}


def _state_payload(state) -> dict:
    return {"schema": state.get("schema", {}), "values": state.get("values", {}), "armed": is_armed(state)}


async def _ensure_seed(conversation_id: str, state):
    if state is not None:
        return state
    state = seed()
    await set_workflow_state(conversation_id, WORKFLOW_ID, state)
    return state


async def pre_pipeline(ctx):
    """One automatic analysis attempt per conversation, opt-in via ``auto_analyze``.

    Gated on ``is_armed OR auto_analyzed`` so it fires exactly once when the spec
    is still empty, then never again automatically -- whether or not that attempt
    produced anything -- which bounds the per-turn cost. Manual re-analysis is the
    refresh path.
    """
    cfg = await get_workflow_config(WORKFLOW_ID)
    if not cfg.get("auto_analyze"):
        return
    state = await _ensure_seed(ctx.conversation_id, await get_workflow_state(ctx.conversation_id, WORKFLOW_ID))
    if is_armed(state) or state.get("auto_analyzed"):
        return

    reasoning_on = bool(cfg.get("reasoning", False))
    pass_id = f"{WORKFLOW_ID}:analyze" if cfg.get("stream_reasoning") else None
    values: dict = {}
    async for ev in run_analyzer(
        ctx,
        state.get("schema", {}),
        pass_id=pass_id,
        kv_tracker=ctx.kv_tracker,
        reasoning_on=reasoning_on,
        client=ctx.client,
    ):
        if ev.get("type") == "result":
            values = ev["values"]
        else:
            yield ev
    merged = {**state, "values": {**state.get("values", {}), **values}, "auto_analyzed": True}
    await set_workflow_state(ctx.conversation_id, WORKFLOW_ID, merged)


async def post_pipeline(ctx):
    """Run the judge/enforce loop on the draft when the spec is armed.

    Dormant otherwise (no LLM call). The phase pill is yielded here, around the
    loop, so it clears even when the loop made no edit; the loop's own events are
    re-yielded and its final draft is read off the terminal sentinel.
    """
    state = await get_workflow_state(ctx.conversation_id, WORKFLOW_ID)
    if state is None or not is_armed(state):
        return

    cfg = await get_workflow_config(WORKFLOW_ID)
    n = _as_n(cfg.get("max_iterations", 1))
    mode = cfg.get("prompt_mode") or "minimal"
    reasoning_on = bool(cfg.get("reasoning", False))
    stream = bool(cfg.get("stream_reasoning", False))
    spec = filled_elements(state)

    judge_fn = make_judge_fn(ctx, spec, mode, reasoning_on, stream)
    enforce_fn = make_enforce_fn(ctx, spec, mode, reasoning_on, stream)

    def is_aborted():
        return ctx.client.is_aborted

    yield {"event": "phase_status", "data": {"channel": f"workflow:{WORKFLOW_ID}", "label": "Enforcing format"}}
    final = ctx.draft
    async for ev in run_enforcement_loop(ctx.draft, n, judge_fn, enforce_fn, apply_patches, is_aborted):
        if ev.get("type") == "loop_done":
            final = ev["draft"]
        else:
            yield ev
    if final != ctx.draft:
        yield {"type": EV_DRAFT_REPLACED, "draft": final}
    yield {"event": "phase_status", "data": {"channel": f"workflow:{WORKFLOW_ID}", "state": "done"}}


async def on_demand(ctx, body):
    """Menu RPC dispatched on ``body['action']``. Seeds state on first contact so
    the menu always has a schema to show."""
    action = body.get("action") if isinstance(body, dict) else None
    state = await _ensure_seed(ctx.conversation_id, await get_workflow_state(ctx.conversation_id, WORKFLOW_ID))

    if action == "get":
        return _state_payload(state)

    if action == "save":
        new = dict(state)
        if isinstance(body.get("schema"), dict):
            new["schema"] = _str_map(body["schema"])
        if isinstance(body.get("values"), dict):
            new["values"] = _str_map(body["values"])
        await set_workflow_state(ctx.conversation_id, WORKFLOW_ID, new)
        return _state_payload(new)

    if action == "analyze":
        # Reasoning (the "think" knob) applies to every agent, this one included.
        # On-demand has no SSE stream, so the reasoning is never surfaced here
        # regardless of stream_reasoning (pass_id stays None) -- the model still
        # thinks when reasoning is on. auto_analyzed is left untouched: this is the
        # manual refresh, not the one auto attempt.
        #
        # Off-turn tool-calling work runs on the agent lane, matching the other
        # off-turn workflow hooks. In a dual-model setup that puts this analyzer on
        # a different model than the pre-pipeline one, which reads the same prose
        # and writes the same slot: PreCtx carries no agent lane, so the automatic
        # attempt cannot follow. The two can therefore record different conventions
        # for one conversation.
        cfg = await get_workflow_config(WORKFLOW_ID)
        reasoning_on = bool(cfg.get("reasoning", False))
        values: dict = {}
        async for ev in run_analyzer(
            ctx,
            state.get("schema", {}),
            pass_id=None,
            kv_tracker=None,
            reasoning_on=reasoning_on,
            client=ctx.agent_client,
            model_name=ctx.agent_model_name,
        ):
            if ev.get("type") == "result":
                values = ev["values"]
        merged = {**state, "values": {**state.get("values", {}), **values}}
        await set_workflow_state(ctx.conversation_id, WORKFLOW_ID, merged)
        return _state_payload(merged)

    if action == "reset":
        new = seed()
        await set_workflow_state(ctx.conversation_id, WORKFLOW_ID, new)
        return _state_payload(new)

    return {"error": "unknown action"}
