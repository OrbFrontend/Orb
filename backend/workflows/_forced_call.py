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
from types import MappingProxyType
from typing import Any, AsyncIterator, Mapping, Sequence

from ..inference import (
    STANDALONE_TOOLS,
    TOOLS,
    enabled_schemas,
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
    kv_tracker: Any = None,
    reasoning_on: bool = True,
    temperature: float = 0.25,
    max_tokens: int = 8192,
) -> AsyncIterator[dict]:
    """Force one tool call and yield its parsed arguments.

    Reasoning deltas yield as ``{"event": "reasoning", "data": {"pass":
    pass_id, "delta": ...}}`` when ``pass_id`` is set (the orchestrator
    forwards these to SSE); they are suppressed when ``pass_id`` is None
    so on-demand handlers without an SSE stream do not need to filter.
    The terminal event is always ``{"type": "result", "args": <dict>}``.

    Tools assembly:
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
    """
    schema = TOOLS[tool_name]["schema"]
    if enabled_tools is None:
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
            model=settings.get("model_name", ""),
        )

    reasoning_params = reasoning_cfg(reasoning_on)
    resp: dict = {}
    try:
        async for event in client.complete(
            messages=messages,
            model=settings["model_name"],
            tools=tools,
            tool_choice=TOOLS[tool_name]["choice"],
            temperature=temperature,
            max_tokens=max_tokens,
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
    except Exception as e:
        logger.warning("forced_tool_call %s failed during stream: %r", tool_name, e)
        yield {"type": "result", "args": {}}
        return

    try:
        calls = parse_tool_calls(resp)
        args = next((c["arguments"] for c in calls if c["name"] == tool_name), {})
    except Exception as e:
        logger.warning("forced_tool_call %s parse failed: %r", tool_name, e)
        args = {}

    yield {"type": "result", "args": args}
