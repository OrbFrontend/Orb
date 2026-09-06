"""Run a workflow's forced tool call and return its arguments."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from ..core import ReasoningChannel, agent_lane_max_tokens, mark_call_start
from ..inference import (
    STANDALONE_TOOLS,
    TOOLS,
    enabled_schemas,
    honors_forced_tool_choice,
    note_forced_tool_choice_ignored,
    parse_tool_calls,
    reasoning_cfg,
)

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


async def forced_tool_call(
    *,
    client: Any,
    prefix: Sequence[dict],
    tail_messages: Sequence[dict],
    tool_name: str,
    settings: Mapping[str, Any],
    pass_id: str | None = None,
    enabled_tools: Mapping[str, bool] | None = None,
    schema_overrides: Mapping[str, Mapping] | None = None,
    offer_tools: Sequence[str] | None = None,
    kv_tracker: Any = None,
    model_name: str | None = None,
    reasoning_on: bool = True,
    temperature: float = 0.25,
    token_floor: int = 8192,
    tools_in_prompt: bool = True,
) -> AsyncIterator[dict]:
    """Run one forced tool call and yield its parsed arguments.

    ``token_floor`` is what this call needs to answer in full; the agent lane's
    configured ``max_tokens`` raises it when the user has given that endpoint more
    room (see :func:`~backend.core.agent_lane_max_tokens`), the same floor the
    Director and Editor forced calls apply. ``temperature`` stays a caller
    constant: a forced call fills a schema, so a roleplay preset would only add
    flourish to it -- the same split ``features.cards._drafting`` documents.
    """
    schema = TOOLS[tool_name]["schema"]
    resolved_model = model_name or settings["model_name"]
    reasoning_params = reasoning_cfg(reasoning_on)
    base_url = getattr(client, "base_url", "")
    # Only an offer_tools array may be collapsed to the forced tool: it exists
    # for cache reuse, not for the model to choose from. The enabled_tools array
    # is the pipeline's byte-identical blob -- shrinking that would break the
    # cross-pass KV prefix, which outranks any single call's tool selection.
    collapsible = offer_tools is not None
    if offer_tools is not None:
        # Fixed, order-stable blob shared verbatim across sibling forced calls
        # (image_gen's analyze + compose). A provider that rejects response_format
        # json_schema (DeepSeek) can't be forced promptlessly and must keep tools
        # in the body; sending the identical blob on both calls -- order fixed
        # regardless of which is forced, only tool_choice differs -- is what lets
        # them reuse each other's cached prefix *where the backend renders the
        # whole array*. Standalone tools stay out of enabled_schemas; the caller
        # names them here rather than leaking them into the pipeline's tool set.
        #
        # Measured caveat (2026-08-04, docs/architecture/kv-cache.md Invariant 3):
        # honoring a forced tool_choice and rendering the whole array are
        # INDEPENDENT properties, and several backends do the first by doing the
        # opposite of the second -- they serialize only the forced tool. On
        # Gemma-4-26B @ Ionstream `offer_tools` + forced(analyze_scene) renders
        # byte-identically to shipping [analyze_scene] alone; DeepSeek v4-pro is
        # the same plus a ~7-token forcing directive. There the two calls share
        # only the conversation body, never the blob, so this array buys nothing.
        # It is kept because it costs nothing to send and does pay off on
        # backends that render the array whole (Gemma-4-31B @ CoreWeave, OpenAI),
        # and the loss where it doesn't is bounded to the blob -- a few hundred
        # tokens per image, not a prefix bust. Do not infer from a working forced
        # call that the sibling reuse is happening.
        tools = [TOOLS[n]["schema"] for n in offer_tools]
        if schema not in tools:
            tools.append(schema)
        # ...unless the wire won't carry the forcing. Then a rival schema in the
        # array is a lottery the caller never asked for: with compose_image_prompt
        # forced but coerced, deepseek-v4-pro answered with analyze_scene 8/8 --
        # no arguments for the tool that was asked for. Ship only the forced tool
        # in that case: the shared blob is a cache optimization, calling the right
        # tool is the point of the call. Providers that ignore the field silently
        # are learned from the reply below rather than listed here.
        if not honors_forced_tool_choice(base_url, resolved_model, reasoning_params):
            tools = [schema]
    elif enabled_tools is None:
        tools = [schema]
    else:
        overrides_arg = _plain(schema_overrides) if schema_overrides else None
        tools = list(enabled_schemas(dict(enabled_tools), overrides_arg))
        canonical = (overrides_arg or {}).get(tool_name, schema)
        if canonical is not None and (tool_name in STANDALONE_TOOLS or canonical not in tools):
            tools.append(canonical)

    messages = [_plain(m) for m in prefix] + [_plain(m) for m in tail_messages]

    kv_label = pass_id or f"forced:{tool_name}"
    if kv_tracker is not None:
        kv_tracker.record(
            kv_label,
            messages,
            tools,
            model=resolved_model,
        )

    resp: dict = {}
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
                tool_choice=TOOLS[tool_name]["choice"],
                temperature=temperature,
                max_tokens=agent_lane_max_tokens(settings, floor=token_floor),
                tools_in_prompt=tools_in_prompt,
                **reasoning_params,
            )
        ):
            etype = event.get("type")
            if etype == "reasoning":
                if pass_id is not None:
                    yield {
                        "event": "reasoning",
                        "data": {"pass": pass_id, "delta": reasoning.push(event)},
                    }
            elif etype == "done":
                resp = event.get("message", {}) or {}
                if kv_tracker is not None:
                    kv_tracker.record_usage(kv_label, event.get("usage"))

    def _parse() -> tuple[dict, bool]:
        """(the forced tool's arguments, whether some *other* tool was called).

        The second flag is the only sound evidence that tool selection was left
        to the model: a reply with no call at all proves nothing (truncated at
        the token budget mid-reasoning, a content-only answer, a provider-side
        finish_reason=error), and treating it as evidence would drop the shared
        blob for the whole session over one flaky reply.
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
            # A different tool came back: the forced tool_choice did not take.
            # Some providers ignore the field instead of rejecting it (OpenRouter
            # routing a thinking-on model, llama.cpp's chat endpoint), so nothing
            # up front can predict it -- the reply is the only evidence. Remember
            # the pair so the rest of the session skips the lottery, and retry now
            # with the forced tool alone: that rules out the wrong tool, though a
            # provider free to call nothing at all can still answer without a call
            # (the empty-args degrade below covers that).
            note_forced_tool_choice_ignored(base_url, resolved_model)
            logger.info(
                "forced_tool_call %s: %s ignored the forced tool_choice; retrying with that tool alone",
                tool_name,
                resolved_model,
            )
            tools = [schema]
            if kv_tracker is not None:
                kv_tracker.record(kv_label, messages, tools, model=resolved_model)
            async for event in _attempt(tools):
                yield event
            args, _ = _parse()
    except Exception as e:
        logger.warning("forced_tool_call %s failed during stream: %r", tool_name, e)
        yield {"type": "result", "args": {}}
        return

    yield {"type": "result", "args": args}
