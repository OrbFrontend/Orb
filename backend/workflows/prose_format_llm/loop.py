"""The judge/enforce loop and the three forced-tool paths.

``run_enforcement_loop`` is the orchestration (pure control flow over injected
callables). ``make_judge_fn`` / ``make_enforce_fn`` / ``run_analyzer`` are the
paths that actually call the model via ``forced_tool_call``, shaped per the
selected prompt mode.
"""

from __future__ import annotations

import logging

from ..toolkit import forced_tool_call
from . import TOOL_ANALYZE, TOOL_PATCH, TOOL_REPORT, WORKFLOW_ID
from .prompts import (
    ANALYZER_PREAMBLE,
    ENFORCER_PREAMBLE,
    JUDGE_PREAMBLE,
    analyze_instruction,
    enforce_instruction,
    judge_instruction,
    render_spec_block,
)
from .violations import clean_analyzer_records, validate_violations

logger = logging.getLogger(__name__)

# Judge classifies, so run it deterministic -- a stable count is what lets the
# no-progress guard mean something. The enforcer rewrites a span and gets a
# little headroom.
_JUDGE_TEMPERATURE = 0.0
_ENFORCE_TEMPERATURE = 0.2
_ANALYZE_TEMPERATURE = 0.0


def _rail(step: str, i: int, payload: list) -> dict:
    """A one-line rail summary, emitted regardless of the ``reasoning`` config.

    Shaped as an ``event`` (not a ``type``) so the bridge forwards it to SSE
    rather than dropping it as an unknown control event. Each delta ends with a
    newline because the frontend rail concatenates deltas without separators.
    """
    pass_id = f"{WORKFLOW_ID}:enforce" if step.startswith("enforce") else f"{WORKFLOW_ID}:judge"
    if step == "judge":
        cats = ", ".join(sorted({v["category"] for v in payload}))
        delta = f"judge iter {i}: {len(payload)} violations" + (f" [{cats}]" if cats else "") + "\n"
    elif step == "enforce":
        delta = f"enforce iter {i}: {len(payload)} patches\n"
    else:  # enforce_errors
        delta = f"enforce iter {i}: {len(payload)} patch(es) skipped\n"
    return {"event": "reasoning", "data": {"pass": pass_id, "delta": delta}}


async def run_enforcement_loop(draft, n, judge_fn, enforce_fn, apply_fn, is_aborted):
    """Judge -> (enforce -> re-judge)* up to *n* times, yielding rail/reasoning
    events as it goes and finishing with ``{"type":"loop_done","draft":...}``.

    An async generator, not a coroutine: a workflow hook's only channel to SSE
    is what it yields, so the final draft rides a terminal sentinel rather than a
    return value. Stops on a clean judge, no patches, no progress (the count
    failed to drop -- guards against an enforcer that thrashes), abort, or the
    cap. ``n == 0`` judges once for diagnosis and never edits.
    """
    violations: list = []
    async for ev in judge_fn(draft):
        if ev.get("type") == "result":
            violations = ev["violations"]
        else:
            yield ev
    yield _rail("judge", 0, violations)

    if violations and n > 0:
        prev = len(violations)
        for i in range(n):
            if is_aborted():
                break
            patches: list = []
            async for ev in enforce_fn(draft, violations):
                if ev.get("type") == "result":
                    patches = ev["patches"]
                else:
                    yield ev
            yield _rail("enforce", i + 1, patches)
            if not patches:
                break
            draft, errs = apply_fn(draft, patches)
            if errs:
                for e in errs:
                    logger.info("prose_format_llm enforce iter %d: %s", i + 1, e)
                yield _rail("enforce_errors", i + 1, errs)
            violations = []
            async for ev in judge_fn(draft):
                if ev.get("type") == "result":
                    violations = ev["violations"]
                else:
                    yield ev
            yield _rail("judge", i + 1, violations)
            if not violations or len(violations) >= prev:
                break
            prev = len(violations)

    yield {"type": "loop_done", "draft": draft}


def _forced(ctx, mode, prefix, tail, tool_name, pass_id, reasoning_on, temperature):
    """Drive one forced tool call, wired for KV reuse per prompt mode.

    ``extend`` reproduces the pipeline's warm prefix + tools blob; ``minimal``
    sends its own small self-contained prefix and a single-tool array.
    """
    common = dict(
        client=ctx.client,
        prefix=prefix,
        tail_messages=tail,
        tool_name=tool_name,
        settings=ctx.settings,
        pass_id=pass_id,
        kv_tracker=ctx.kv_tracker,
        reasoning_on=reasoning_on,
        temperature=temperature,
    )
    if mode == "extend":
        return forced_tool_call(enabled_tools=ctx.enabled_tools, schema_overrides=ctx.schema_overrides, **common)
    return forced_tool_call(enabled_tools=None, **common)


def _spec_messages(ctx, mode, preamble, spec_block, instruction, draft):
    """Build (prefix, tail) for a judge/enforce call.

    ``minimal``: preamble + spec ride a fresh system prefix, the draft rides the
    tail. ``extend``: the draft is replayed as an assistant turn over the
    pipeline's prefix, and the preamble/spec/instruction ride the final user turn.
    """
    if mode == "extend":
        tail = [
            {"role": "user", "content": ctx.effective_msg},
            {"role": "assistant", "content": draft},
            {
                "role": "user",
                "content": f"{preamble}\n\nRecorded prose format:\n{spec_block}\n\n{instruction}\n\n(The draft is the assistant reply directly above.)",
            },
        ]
        return ctx.prefix, tail
    prefix = [{"role": "system", "content": f"{preamble}\n\nRecorded prose format:\n{spec_block}"}]
    tail = [{"role": "user", "content": f"{instruction}\n\nDraft:\n{draft}"}]
    return prefix, tail


def make_judge_fn(ctx, spec, mode, reasoning_on, stream):
    """A factory returning a fresh judge async generator per call (one per loop
    iteration). ``reasoning_on`` makes the model think; ``stream`` independently
    decides whether that thinking is surfaced to the rail (via ``pass_id``). Yields
    the model's reasoning when streamed, then a terminal
    ``{"type":"result","violations":[...]}`` of validated violations."""
    spec_block = render_spec_block(spec)
    filled_keys = list(spec.keys())
    pass_id = f"{WORKFLOW_ID}:judge" if stream else None

    async def judge_fn(draft):
        prefix, tail = _spec_messages(ctx, mode, JUDGE_PREAMBLE, spec_block, judge_instruction(TOOL_REPORT), draft)
        args: dict = {}
        async for ev in _forced(ctx, mode, prefix, tail, TOOL_REPORT, pass_id, reasoning_on, _JUDGE_TEMPERATURE):
            if ev.get("type") == "result":
                args = ev["args"]
            else:
                yield ev
        yield {"type": "result", "violations": validate_violations(args.get("violations"), draft, filled_keys)}

    return judge_fn


def make_enforce_fn(ctx, spec, mode, reasoning_on, stream):
    """A factory returning a fresh enforcer async generator per call.
    ``reasoning_on`` makes the model think; ``stream`` gates surfacing (``pass_id``).
    Yields reasoning when streamed, then a terminal ``{"type":"result","patches":[...]}``."""
    spec_block = render_spec_block(spec)
    pass_id = f"{WORKFLOW_ID}:enforce" if stream else None

    async def enforce_fn(draft, violations):
        instruction = enforce_instruction(violations, TOOL_PATCH)
        prefix, tail = _spec_messages(ctx, mode, ENFORCER_PREAMBLE, spec_block, instruction, draft)
        args: dict = {}
        async for ev in _forced(ctx, mode, prefix, tail, TOOL_PATCH, pass_id, reasoning_on, _ENFORCE_TEMPERATURE):
            if ev.get("type") == "result":
                args = ev["args"]
            else:
                yield ev
        patches = args.get("patches")
        yield {"type": "result", "patches": patches if isinstance(patches, list) else []}

    return enforce_fn


async def run_analyzer(ctx, schema, *, pass_id, kv_tracker, reasoning_on, client, model_name=None):
    """Infer the convention from recent prose and yield a terminal
    ``{"type":"result","values":{...}}``.

    Self-contained (own system prefix, history-derived samples) so the PRE and
    on-demand paths produce the same prompt -- ``OnDemandCtx`` exposes no pipeline
    prefix or kv_tracker, and the analyzer runs at most once per conversation, so
    pipeline cache reuse is moot.

    The lane is the caller's to choose rather than read off ``ctx``: only the
    off-turn contexts resolve an agent lane, so the in-turn caller has no choice
    to make and passes its single client with ``model_name=None`` (which falls
    back to ``settings["model_name"]``).
    """
    prefix = [{"role": "system", "content": ANALYZER_PREAMBLE}]
    tail = [{"role": "user", "content": analyze_instruction(schema, ctx.history, TOOL_ANALYZE)}]
    args: dict = {}
    async for ev in forced_tool_call(
        client=client,
        prefix=prefix,
        tail_messages=tail,
        tool_name=TOOL_ANALYZE,
        settings=ctx.settings,
        pass_id=pass_id,
        enabled_tools=None,
        kv_tracker=kv_tracker,
        model_name=model_name,
        reasoning_on=reasoning_on,
        temperature=_ANALYZE_TEMPERATURE,
    ):
        if ev.get("type") == "result":
            args = ev["args"]
        else:
            yield ev
    keys = list(schema.keys()) if isinstance(schema, dict) else []
    yield {"type": "result", "values": clean_analyzer_records(args.get("records"), keys)}
