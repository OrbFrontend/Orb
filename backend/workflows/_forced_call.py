"""Run a workflow's forced tool call and return its arguments."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from ..core import AssistantToolMessage, ReasoningChannel, agent_lane_max_tokens, mark_call_start
from ..core.llm_types import CompletionMessage
from ..core.settings import Settings
from ..inference import (
    KVCacheTracker,
    LLMClient,
    honors_forced_tool_choice,
    note_forced_tool_choice_ignored,
    parse_tool_calls,
    reasoning_cfg,
    replay_reasoning,
)
from ..prompting.tool_catalog import enabled_schemas, is_standalone_tool, require_tool

logger = logging.getLogger(__name__)


def _plain(obj: Any) -> Any:
    """Strip read-only wrappers so json can serialize the value.

    Workflows may pass ``pre_ctx.prefix`` and ``pre_ctx.history`` slices
    directly (recursively wrapped to ``tuple`` of ``MappingProxyType`` of
    ...). The KV tracker's ``record`` and the LLM client's ``complete``
    both run ``json.dumps`` over the assembled messages; that call fails
    on ``MappingProxyType`` and ``frozenset``. Unwrap here so the bytes
    match what the pipeline itself would serialize.
    """
    if isinstance(obj, MappingProxyType):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (tuple, list)):
        return [_plain(v) for v in obj]
    if isinstance(obj, frozenset):
        return [_plain(v) for v in obj]
    return obj


def _replay(resp: Mapping[str, Any], tool_name: str, args: Mapping[str, Any], call_id: str) -> AssistantToolMessage:
    """The reply as a structured assistant turn, for a caller that continues the thread.

    The id is assigned here because structured forced calls tend to come back as ``call_0``: a thread of several must answer
    each call exactly once. Content is kept only beside native ``tool_calls``; a call parsed out of the content body would
    otherwise appear twice.
    """
    return {
        "role": "assistant",
        "content": (resp.get("content") or "") if resp.get("tool_calls") else "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(dict(args), ensure_ascii=False)},
            }
        ],
        **replay_reasoning(resp),
    }


async def forced_tool_call(
    *,
    client: LLMClient,
    prefix: Sequence[Mapping[str, Any]],
    tail_messages: Sequence[Mapping[str, Any]],
    tool_name: str,
    settings: Settings,
    pass_id: str | None = None,
    enabled_tools: Mapping[str, bool] | None = None,
    schema_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    offer_tools: Sequence[str] | None = None,
    kv_tracker: KVCacheTracker | None = None,
    cache_shape: str = "",
    model_name: str | None = None,
    reasoning_on: bool = True,
    temperature: float = 0.25,
    tools_in_prompt: bool = True,
    call_id: str | None = None,
    raise_errors: bool = False,
) -> AsyncIterator[dict]:
    """Run a forced call, yielding parsed arguments with the Agent token budget and caller-defined temperature.

    Standalone prompt families require cache_shape; conversation extensions leave it empty. call_id includes a replayable
    assistant turn when arguments exist. Failures default to empty arguments; raise_errors preserves provider errors after
    transient retries.
    """
    tool = require_tool(tool_name)
    schema = tool["schema"]
    resolved_model = model_name or settings["model_name"]
    reasoning_params = reasoning_cfg(reasoning_on)
    base_url = getattr(client, "base_url", "")
    # Only an offer_tools array may be collapsed to the forced tool: it exists for cache reuse, not for the model to choose
    # from. The enabled_tools array is the pipeline's byte-identical blob -- shrinking that would break the cross-pass KV
    # prefix, which outranks any single call's tool selection.
    collapsible = offer_tools is not None
    if offer_tools is not None:
        # Share an order-stable tool array across sibling forced calls; only tool_choice varies. Standalone tools stay out of
        # enabled_schemas. Prefix reuse depends on provider rendering: some serialize only the forced tool, sharing the
        # conversation body but not the blob. A working forced call does not prove sibling cache reuse (see
        # docs/architecture/kv-cache.md, Invariant 3).
        tools = [require_tool(name)["schema"] for name in offer_tools]
        if schema not in tools:
            tools.append(schema)
        # ...unless the wire won't carry the forcing. Then a rival schema in the array is a lottery the caller never asked for:
        # with compose_image_prompt forced but coerced, a model can answer with the selector instead -- no arguments for the
        # tool that was asked for. Ship only the forced tool in that case: the shared blob is a cache optimization, calling the
        # right tool is the point of the call. Providers that ignore the field silently are learned from the reply below rather
        # than listed here.
        if not honors_forced_tool_choice(base_url, resolved_model, reasoning_params):
            tools = [schema]
    elif enabled_tools is None:
        tools = [schema]
    else:
        overrides_arg = _plain(schema_overrides) if schema_overrides else None
        tools = list(enabled_schemas(dict(enabled_tools), overrides_arg))
        canonical = (overrides_arg or {}).get(tool_name, schema)
        if canonical is not None and (is_standalone_tool(tool_name) or canonical not in tools):
            tools.append(canonical)

    messages = [_plain(m) for m in prefix] + [_plain(m) for m in tail_messages]

    kv_label = pass_id or f"forced:{tool_name}"
    if kv_tracker is not None:
        kv_tracker.record(kv_label, messages, tools, model=resolved_model, endpoint=base_url, shape=cache_shape)

    resp: CompletionMessage = {}
    # Keep retries in the same workflow buffer so their reasoning is separated.
    reasoning = ReasoningChannel()

    async def _attempt(tool_array: list[dict]) -> AsyncIterator[dict]:
        nonlocal resp
        resp = {}
        async for event in mark_call_start(
            client.complete(
                messages=messages,
                model=resolved_model,
                tools=tool_array,
                tool_choice=tool["choice"],
                temperature=temperature,
                max_tokens=agent_lane_max_tokens(settings),
                tools_in_prompt=tools_in_prompt,
                **reasoning_params,
            )
        ):
            etype = event.get("type")
            if etype == "reasoning":
                if pass_id is not None:
                    yield {"event": "reasoning", "data": {"pass": pass_id, "delta": reasoning.push(event)}}
            elif etype == "done":
                resp = event.get("message", {}) or {}
                if kv_tracker is not None:
                    kv_tracker.record_usage(kv_label, event.get("usage"))

    def _parse() -> tuple[dict, bool]:
        """(the forced tool's arguments, whether some *other* tool was called).

        The second flag is the only sound evidence that tool selection was left to the model: a reply with no call at all proves
        nothing (truncated at the token budget mid-reasoning, a content-only answer, a provider-side finish_reason=error), and
        treating it as evidence would drop the shared blob for the whole session over one flaky reply.
        """
        try:
            calls = parse_tool_calls(resp)
        except Exception as e:
            logger.warning("forced_tool_call %s parse failed: %r", tool_name, e)
            return {}, False
        mine = [c for c in calls if c["name"] == tool_name]
        return (mine[0]["arguments"] if mine else {}), bool(calls) and not mine

    try:
        async for event in _attempt(tools):
            yield event
        args, wrong_tool = _parse()
        if wrong_tool and collapsible and len(tools) > 1:
            # A different tool came back: the forced tool_choice did not take. Some providers ignore the field instead of
            # rejecting it (OpenRouter routing a thinking-on model, llama.cpp's chat endpoint), so nothing up front can predict
            # it -- the reply is the only evidence. Remember the pair so the rest of the session skips the lottery, and retry
            # now with the forced tool alone: that rules out the wrong tool, though a provider free to call nothing at all can
            # still answer without a call (the empty-args degrade below covers that).
            note_forced_tool_choice_ignored(base_url, resolved_model)
            logger.info(
                "forced_tool_call %s: %s ignored the forced tool_choice; retrying with that tool alone",
                tool_name,
                resolved_model,
            )
            tools = [schema]
            if kv_tracker is not None:
                kv_tracker.record(kv_label, messages, tools, model=resolved_model, endpoint=base_url, shape=cache_shape)
            async for event in _attempt(tools):
                yield event
            args, _ = _parse()
    except Exception as e:
        logger.warning("forced_tool_call %s failed during stream: %r", tool_name, e)
        if raise_errors:
            raise
        yield {"type": "result", "args": {}}
        return

    result: dict = {"type": "result", "args": args}
    if call_id is not None and args:
        result["replay"] = _replay(resp, tool_name, args, call_id)
    yield result
