"""One-shot forced tool call helper for workflow authors.

Wraps a single ``client.complete(tool_choice=...)`` invocation: assembles
the tools array, optionally records the call against a KV-cache tracker,
forwards reasoning deltas to the SSE stream when the caller supplied a
``pass_id``, and yields exactly one terminal ``{"type": "result", "args":
<dict>}`` event. Never raises -- tool-call missing, parse failure, or any
network error all degrade to ``{"type": "result", "args": {}}`` so a
single bad LLM reply cannot crash a workflow.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

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
    max_tokens: int = 8192,
    tools_in_prompt: bool = True,
) -> AsyncIterator[dict]:
    """Force one tool call and yield its parsed arguments.

    Reasoning deltas yield as ``{"event": "reasoning", "data": {"pass":
    pass_id, "delta": ...}}`` when ``pass_id`` is set (the orchestrator
    forwards these to SSE); they are suppressed when ``pass_id`` is None
    so on-demand handlers without an SSE stream do not need to filter.
    The terminal event is always ``{"type": "result", "args": <dict>}``.

    Tools assembly:
      - ``offer_tools=<names>`` -- fixed, order-stable array of those
        registry tools (forced ``tool_name`` appended if absent). Use to
        share one blob across sibling forced calls so a provider that must
        keep tools in the body still lets them reuse each other's cached
        prefix. Takes precedence over ``enabled_tools``. Collapses to the
        single forced tool on providers that don't honor forcing (known up
        front via ``honors_forced_tool_choice``, or learned from a reply that
        called some other tool) -- an unforced array picks its own tool.
      - ``enabled_tools=None`` -- single-tool array. Smallest bytes; use
        when the caller does not need pipeline tools-bytes cache reuse.
      - ``enabled_tools=<dict>`` -- assemble the same tools array
        ``enabled_schemas(enabled_tools, schema_overrides)`` returns. If
        ``tool_name`` is standalone or otherwise absent from the result,
        append its schema so the forced ``tool_choice`` resolves.
        ``schema_overrides`` must be the dict the pipeline shipped this
        turn (``pre_ctx.schema_overrides`` / ``post_ctx.schema_overrides``)
        for byte-identical tools cache reuse.

    ``kv_tracker=None`` skips the per-call ``record(...)`` + ``record_usage(...)``
    steps silently (the on-demand path does not participate in turn caching).

    ``model_name`` overrides ``settings["model_name"]`` for callers on another
    resolved lane. The same value labels the KV tracker and the wire request.

    ``tools_in_prompt=False`` forwards the client flag of the same name: the
    prompt must not carry the tool schemas (text mode never renders them; chat
    mode then forces via ``response_format`` and omits ``tools`` from the body).
    Use it for off-turn calls that ride a cached conversation prefix, so the
    forced tool's schema cannot perturb the server-rendered prompt bytes.
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
        # regardless of which is forced, only tool_choice differs -- lets them
        # reuse each other's cached conversation prefix. Standalone tools stay out
        # of enabled_schemas; the caller names them here rather than leaking them
        # into the pipeline's tool set.
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

    async def _attempt(tool_array: list[dict]) -> AsyncIterator[dict]:
        nonlocal resp
        resp = {}
        async for event in client.complete(
            messages=messages,
            model=resolved_model,
            tools=tool_array,
            tool_choice=TOOLS[tool_name]["choice"],
            temperature=temperature,
            max_tokens=max_tokens,
            tools_in_prompt=tools_in_prompt,
            **reasoning_params,
        ):
            etype = event.get("type")
            if etype == "reasoning":
                if pass_id is not None:
                    yield {
                        "event": "reasoning",
                        "data": {"pass": pass_id, "delta": event.get("delta", "")},
                    }
            elif etype == "done":
                resp = event.get("message", {}) or {}
                if kv_tracker is not None:
                    kv_tracker.record_usage(kv_label, event.get("usage"))

    def _parse() -> tuple[bool, dict]:
        """(was the forced tool actually called, its arguments)."""
        try:
            calls = parse_tool_calls(resp)
        except Exception as e:
            logger.warning("forced_tool_call %s parse failed: %r", tool_name, e)
            return False, {}
        mine = [c for c in calls if c["name"] == tool_name]
        return bool(mine), (mine[0]["arguments"] if mine else {})

    try:
        async for event in _attempt(tools):
            yield event
        called, args = _parse()
        if not called and collapsible and len(tools) > 1:
            # The forced tool_choice did not take: some providers ignore the
            # field instead of rejecting it (OpenRouter routing a thinking-on
            # model, llama.cpp's chat endpoint), so nothing up front can predict
            # it -- the reply is the only evidence. Remember the pair so the rest
            # of the session skips the lottery, and retry now with the forced tool
            # alone, which is forcing by construction.
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
            _, args = _parse()
    except Exception as e:
        logger.warning("forced_tool_call %s failed during stream: %r", tool_name, e)
        yield {"type": "result", "args": {}}
        return

    yield {"type": "result", "args": args}
