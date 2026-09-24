"""Accumulate a streamed OpenAI-compatible chat completion.

The parsers here read already-framed SSE ``data:`` payloads, so they can be
driven from a list in tests; the HTTP, abort race and retry policy stay in
:mod:`client`. The Anthropic Messages parser lives with its request translation
in :mod:`anthropic` and fills the same :class:`ChatStream`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import text_completion
from .errors import llm_stream_error


@dataclass(slots=True)
class ChatStream:
    """Everything one chat request has streamed so far, in OpenAI message terms."""

    content_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    # The field name the provider streamed reasoning under, replayed as-is.
    reasoning_key: str = "reasoning_content"
    reasoning_details: list[dict] = field(default_factory=list)
    tool_calls: dict[int, dict] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: dict | None = None

    def tool_entry(self, index: int) -> dict:
        """The accumulator slot for tool call *index*, opened on first use."""
        if index not in self.tool_calls:
            self.tool_calls[index] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            }
        return self.tool_calls[index]


def merge_reasoning_details(blocks: list[dict], fragments: object) -> None:
    """Fold one delta's ``reasoning_details`` fragments into whole blocks.

    OpenRouter streams a block as pieces that share an ``index``: text and
    summary arrive in slices, while signature, data and id arrive once. Replaying
    the pieces as separate blocks is rejected, so they are joined back into one
    block per index.
    """
    if not isinstance(fragments, list):
        return
    for fragment in fragments:
        if not isinstance(fragment, Mapping):
            continue
        index = fragment.get("index")
        block = blocks[-1] if blocks and index is not None and blocks[-1].get("index") == index else None
        if block is None or block.get("type") != fragment.get("type"):
            blocks.append(dict(fragment))
            continue
        for key, value in fragment.items():
            if key in {"text", "summary"} and isinstance(value, str):
                block[key] = (block.get(key) or "") + value
            elif value is not None:
                block[key] = value


def parse_chat_logprobs(choice: Mapping[str, Any]) -> list[dict]:
    """Normalize an OpenAI-compat ``choice.logprobs`` block to Orb's prob shape.

    Thin wrapper over :func:`text_completion.normalize_prob_records`: the
    ``logprobs.content`` records carry the same fields as llama.cpp's
    OpenAI-style ``completion_probabilities`` variant, so one normalizer
    serves both transports and the route frames both the same way.
    """
    logprobs = choice.get("logprobs")
    if not isinstance(logprobs, dict):
        return []
    return text_completion.normalize_prob_records(logprobs.get("content"))


async def consume_openai(
    payloads: AsyncIterable[str],
    acc: ChatStream,
    *,
    forced: bool,
    url: str,
    model: str,
    api_key: str,
    is_aborted: Callable[[], bool],
) -> AsyncIterator[dict]:
    """Fold ``/chat/completions`` stream payloads into *acc*, yielding live deltas.

    A *forced* call buffers its content as the tool-arguments payload instead of
    streaming it, so the caller never sees a half-built JSON body. An in-band
    error or a stream without a finish reason or ``[DONE]`` is a provider failure.
    """
    # Slot last handed to an index-less delta; -1 before the first.
    unindexed = -1

    def slot_for(tc_delta: Mapping[str, Any]) -> int:
        """Resolve one tool-call delta to an accumulator slot.

        Google's OpenAI-compatible surface omits ``index`` from
        ``delta.tool_calls`` entirely, so keying on ``index`` with a
        default of 0 merged every parallel call into one entry --
        names concatenated, all but the first argument payload lost.
        Without an index, a delta that STARTS a call (it carries an
        ``id`` or a function ``name``, which the OpenAI contract
        sends only on a call's first chunk) opens the next free
        slot; a bare argument continuation appends to the newest.
        """
        nonlocal unindexed
        index = tc_delta.get("index")
        if isinstance(index, int) and not isinstance(index, bool):
            return index
        function = tc_delta.get("function")
        starts = bool(tc_delta.get("id")) or bool(isinstance(function, Mapping) and function.get("name"))
        if starts or unindexed < 0:
            unindexed = max([*acc.tool_calls, unindexed], default=-1) + 1
        return unindexed

    saw_done = False
    async for payload in payloads:
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        if "error" in chunk or chunk.get("type") == "error":
            raise llm_stream_error(payload=chunk, url=url, model=model, api_key=api_key)
        u = chunk.get("usage")
        if isinstance(u, dict):
            acc.usage = u
        choices = chunk.get("choices") or []
        if not choices:
            continue
        try:
            choice = choices[0]
            delta = choice.get("delta", {})
            rc_key = next((key for key in ("reasoning_content", "reasoning") if delta.get(key)), None)
            if rc_key:
                acc.reasoning_key = rc_key
                acc.reasoning_parts.append(delta[rc_key])
                yield {"type": "reasoning", "delta": delta[rc_key]}
            merge_reasoning_details(acc.reasoning_details, delta.get("reasoning_details"))
            content = delta.get("content")
            if content:
                acc.content_parts.append(content)
                if not forced:
                    yield {"type": "content", "delta": content}
            for rec in parse_chat_logprobs(choice):
                yield {"type": "token_probs", **rec}
            for tc_delta in delta.get("tool_calls") or []:
                entry = acc.tool_entry(slot_for(tc_delta))
                if tc_delta.get("id"):
                    entry["id"] = tc_delta["id"]
                fn = tc_delta.get("function", {})
                if fn.get("name"):
                    entry["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    entry["function"]["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                acc.finish_reason = choice["finish_reason"]
        except (KeyError, IndexError):
            continue
    if not (saw_done or acc.finish_reason or is_aborted()):
        raise llm_stream_error(
            payload={"error": {"message": "OpenAI stream ended before finish_reason or [DONE]"}},
            url=url,
            model=model,
            api_key=api_key,
        )
