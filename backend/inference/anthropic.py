"""Translate Orb's OpenAI-shaped chat contract to Anthropic Messages."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from .chat_stream import ChatStream
from .errors import llm_stream_error
from .schema import strictify_schema

# Anthropic rejects unknown top-level fields. These are the only user-provided
# extra_body keys accepted on a native Messages route; OpenAI-shaped escape
# hatches therefore cannot turn an otherwise-valid request into a hard 400.
# A configured ``cache_control`` (automatic caching) replaces Orb's breakpoints.
EXTRA_BODY_ALLOWED: frozenset[str] = frozenset({"cache_control", "metadata", "service_tier"})
DEFAULT_MAX_TOKENS = 4096

# Sampling support is a capability of the concrete endpoint/model pair, not
# something that can be inferred from a provider-owned model id. Send the
# caller's controls optimistically and remember an explicit rejection.
_SAMPLING_UNSUPPORTED: set[tuple[str, str]] = set()

# Some Messages implementations accept an older ``budget_tokens`` shape and
# reject adaptive thinking or ``output_config.effort``. As with sampling, the
# modern fields go out once and an explicit rejection is remembered.
_THINKING_UNSUPPORTED: set[tuple[str, str]] = set()


def _text_parts(content: object) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
        return []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        kind = part.get("type")
        if kind == "text" and isinstance(part.get("text"), str):
            block: dict[str, Any] = {"type": "text", "text": part["text"]}
            if isinstance(part.get("cache_control"), Mapping):
                block["cache_control"] = dict(part["cache_control"])
            blocks.append(block)
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            if not isinstance(url, str):
                continue
            header, sep, data = url.partition(",")
            if sep and header.startswith("data:") and ";base64" in header:
                media_type = header[5:].split(";", 1)[0]
                try:
                    base64.b64decode(data, validate=True)
                except ValueError:
                    continue
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    }
                )
    return blocks


def _tool_use_blocks(tool_calls: object) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, Sequence) or isinstance(tool_calls, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        function = call.get("function")
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            continue
        raw = function.get("arguments", {})
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = {"_raw": raw}
        else:
            decoded = raw
        if not isinstance(decoded, Mapping):
            decoded = {"value": decoded}
        out.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or ""),
                "name": function["name"],
                "input": dict(decoded),
            }
        )
    return out


def translate_messages(
    messages: Sequence[Mapping[str, Any]],
) -> tuple[str | list[dict[str, Any]], list[dict[str, Any]]]:
    """Hoist system text and translate/coalesce Anthropic user/assistant turns.

    A cache breakpoint on the system text turns ``system`` into a one-block list
    carrying it; the text is the same either way. A breakpoint inside a tool
    message moves onto its ``tool_result`` block.
    """
    system_parts: list[str] = []
    system_cache: dict[str, Any] | None = None
    translated: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if translated and translated[-1]["role"] == role:
            translated[-1]["content"].extend(blocks)
        else:
            translated.append({"role": role, "content": blocks})

    for message in messages:
        role = message.get("role")
        if role == "system":
            for block in _text_parts(message.get("content")):
                text = block.get("text")
                if isinstance(text, str) and text:
                    system_parts.append(text)
                    system_cache = block.get("cache_control") or system_cache
            continue
        if role == "tool":
            content = message.get("content", "")
            tool_content: str | list[dict[str, Any]]
            tool_cache: dict[str, Any] | None = None
            if isinstance(content, str):
                tool_content = content
            else:
                tool_content = _text_parts(content)
                for block in tool_content:
                    tool_cache = block.pop("cache_control", None) or tool_cache
            tool_result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": tool_content,
            }
            if tool_cache:
                tool_result["cache_control"] = tool_cache
            append("user", [tool_result])
            continue
        if role not in {"user", "assistant"}:
            continue
        blocks = _text_parts(message.get("content"))
        if role == "assistant":
            blocks.extend(_tool_use_blocks(message.get("tool_calls")))
        append(role, blocks)
    system = "\n\n".join(system_parts)
    if system and system_cache:
        return [{"type": "text", "text": system, "cache_control": system_cache}], translated
    return system, translated


def translate_tools(tools: object) -> list[dict[str, Any]]:
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        return []
    out: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        function = tool.get("function")
        if not isinstance(function, Mapping) or not isinstance(function.get("name"), str):
            continue
        translated: dict[str, Any] = {
            "name": function["name"],
            # ``strict`` obliges the schema to close every object and mark every
            # property required; Orb's own tools ship partial ``required`` lists,
            # so shape them the way the OpenAI forced path already does rather
            # than sending a schema the API will reject.
            "input_schema": strictify_schema(dict(function.get("parameters") or {"type": "object", "properties": {}})),
            "strict": True,
        }
        if isinstance(function.get("description"), str):
            translated["description"] = function["description"]
        out.append(translated)
    return out


def translate_tool_choice(choice: object) -> dict[str, Any] | None:
    if choice is None:
        return None
    if choice == "auto":
        return {"type": "auto"}
    if choice == "none":
        return {"type": "none"}
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, Mapping):
        function = choice.get("function")
        name = function.get("name") if isinstance(function, Mapping) else None
        if isinstance(name, str) and name:
            return {"type": "tool", "name": name}
        native_type = choice.get("type")
        if native_type in {"auto", "none", "any"}:
            return {"type": native_type}
    return None


def _sampling_allowed(endpoint_url: str, model: str) -> bool:
    return (endpoint_url, model) not in _SAMPLING_UNSUPPORTED


def build_request_body(
    openai_body: Mapping[str, Any],
    endpoint_url: str,
    model: str,
    extra_body: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a native Messages body from an allowlist."""
    system, messages = translate_messages(openai_body.get("messages") or [])
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": openai_body.get("max_tokens") or DEFAULT_MAX_TOKENS,
    }
    if system:
        body["system"] = system
    tools = translate_tools(openai_body.get("tools"))
    if tools:
        body["tools"] = tools
    choice = translate_tool_choice(openai_body.get("tool_choice"))
    if choice is not None and tools:
        body["tool_choice"] = choice

    reasoning = openai_body.get("reasoning")
    thinking = openai_body.get("thinking")
    reasoning_on = (isinstance(reasoning, Mapping) and reasoning.get("enabled") is True) or (
        isinstance(thinking, Mapping) and thinking.get("type") == "enabled"
    )
    if reasoning_on and (endpoint_url, model) not in _THINKING_UNSUPPORTED:
        body["thinking"] = {"type": "adaptive", "display": "summarized"}
        effort = openai_body.get("reasoning_effort")
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            body["output_config"] = {"effort": effort}

    if _sampling_allowed(endpoint_url, model):
        for key in ("temperature", "top_p", "top_k"):
            value = openai_body.get(key)
            if value is not None:
                body[key] = value

    if extra_body:
        for key in EXTRA_BODY_ALLOWED:
            if key in extra_body:
                body[key] = extra_body[key]
    return body


def recover_thinking_error(endpoint_url: str, model: str, body: dict[str, Any], status: int, text: str) -> str | None:
    """Learn a rejection of the 4.6+ reasoning fields and drop them once."""
    if status != 400:
        return None
    low = text.lower()
    present = [key for key in ("thinking", "output_config") if key in body]
    if not present:
        return None
    if not any(key in low for key in (*present, "effort", "budget_tokens")):
        return None
    if not any(marker in low for marker in ("unsupported", "not supported", "not allowed", "extra inputs")):
        return None
    _THINKING_UNSUPPORTED.add((endpoint_url, model))
    for key in present:
        body.pop(key, None)
    return f"Model {model} rejected Anthropic reasoning fields {present}; retrying without them."


def recover_sampling_error(endpoint_url: str, model: str, body: dict[str, Any], status: int, text: str) -> str | None:
    """Learn a sampling-field rejection and remove all three controls once."""
    if status != 400:
        return None
    low = text.lower()
    present = [key for key in ("temperature", "top_p", "top_k") if key in body]
    if not present or not any(key in low for key in present):
        return None
    if not any(marker in low for marker in ("unsupported", "not supported", "not allowed", "extra inputs")):
        return None
    _SAMPLING_UNSUPPORTED.add((endpoint_url, model))
    for key in present:
        body.pop(key, None)
    return f"Model {model} rejected Anthropic sampling fields {present}; retrying without them."


# Messages stop reasons in OpenAI ``finish_reason`` terms; others pass through.
_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


async def consume_stream(
    payloads: AsyncIterable[str],
    acc: ChatStream,
    *,
    forced: bool,
    url: str,
    model: str,
    api_key: str,
    is_aborted: Callable[[], bool],
) -> AsyncIterator[dict]:
    """Fold Messages stream payloads into *acc* in OpenAI terms, yielding live deltas.

    An ``error`` event, or a stream that ends before ``message_stop`` without an
    abort, raises the provider error *url*, *model* and *api_key* describe. A
    *forced* call buffers its text as the tool-arguments payload, as on the
    OpenAI surface.
    """
    stopped = False
    async for payload in payloads:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "ping":
            continue
        if event_type == "error":
            raise llm_stream_error(payload=event, url=url, model=model, api_key=api_key)
        if event_type == "message_start":
            initial = (event.get("message") or {}).get("usage")
            if isinstance(initial, dict):
                acc.usage = dict(initial)
        elif event_type == "content_block_start":
            index = event.get("index", 0)
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                entry = acc.tool_entry(index)
                entry["id"] = block.get("id", "")
                entry["function"]["name"] = block.get("name", "")
                if block.get("input"):
                    entry["function"]["arguments"] = json.dumps(block["input"], separators=(",", ":"))
            elif block.get("type") == "text" and block.get("text"):
                acc.content_parts.append(block["text"])
                if not forced:
                    yield {"type": "content", "delta": block["text"]}
            elif block.get("type") == "thinking" and block.get("thinking"):
                acc.reasoning_parts.append(block["thinking"])
                yield {"type": "reasoning", "delta": block["thinking"]}
        elif event_type == "content_block_delta":
            index = event.get("index", 0)
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta" and delta.get("text"):
                acc.content_parts.append(delta["text"])
                if not forced:
                    yield {"type": "content", "delta": delta["text"]}
            elif delta_type == "thinking_delta" and delta.get("thinking"):
                acc.reasoning_parts.append(delta["thinking"])
                yield {"type": "reasoning", "delta": delta["thinking"]}
            elif delta_type == "input_json_delta" and delta.get("partial_json"):
                acc.tool_entry(index)["function"]["arguments"] += delta["partial_json"]
        elif event_type == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                acc.finish_reason = _FINISH_REASONS.get(stop_reason, stop_reason)
            update = event.get("usage")
            if isinstance(update, dict):
                acc.usage = {**(acc.usage or {}), **update}
        elif event_type == "message_stop":
            stopped = True
            break
    if not stopped and not is_aborted():
        raise llm_stream_error(
            payload={"error": {"message": "Anthropic stream ended before message_stop"}},
            url=url,
            model=model,
            api_key=api_key,
        )
