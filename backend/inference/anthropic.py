"""Translate Orb's OpenAI-shaped chat contract to Anthropic Messages."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from typing import Any

# Anthropic rejects unknown top-level fields. These are the only user-provided
# extra_body keys accepted on a native Messages route; OpenAI-shaped escape
# hatches therefore cannot turn an otherwise-valid request into a hard 400.
EXTRA_BODY_ALLOWED: frozenset[str] = frozenset({"metadata", "service_tier"})
DEFAULT_MAX_TOKENS = 4096

# Current families whose Messages endpoints reject the old sampling controls.
# Unknown proxy model names are tried once and learned from a provider rejection.
_NO_SAMPLING_MARKERS = (
    "opus-5",
    "opus-4-8",
    "opus-4.8",
    "opus-4-7",
    "opus-4.7",
    "sonnet-5",
    "fable-5",
)

_SAMPLING_UNSUPPORTED: set[tuple[str, str]] = set()


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
            blocks.append({"type": "text", "text": part["text"]})
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


def translate_messages(messages: Sequence[Mapping[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Hoist system text and translate/coalesce Anthropic user/assistant turns."""
    system_parts: list[str] = []
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
            continue
        if role == "tool":
            content = message.get("content", "")
            tool_content: str | list[dict[str, Any]]
            if isinstance(content, str):
                tool_content = content
            else:
                tool_content = _text_parts(content)
            append(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": str(message.get("tool_call_id") or ""),
                        "content": tool_content,
                    }
                ],
            )
            continue
        if role not in {"user", "assistant"}:
            continue
        blocks = _text_parts(message.get("content"))
        if role == "assistant":
            blocks.extend(_tool_use_blocks(message.get("tool_calls")))
        append(role, blocks)
    return "\n\n".join(system_parts), translated


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
            "input_schema": dict(function.get("parameters") or {"type": "object", "properties": {}}),
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
    low = model.lower().replace("_", "-")
    return (endpoint_url, model) not in _SAMPLING_UNSUPPORTED and not any(marker in low for marker in _NO_SAMPLING_MARKERS)


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
    if reasoning_on:
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
