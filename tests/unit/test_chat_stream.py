"""Stream parsers driven from payload lists, without an HTTP transport."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from backend.inference import anthropic
from backend.inference.chat_stream import ChatStream, consume_openai
from backend.inference.errors import LLMCallError


async def _payloads(*events: dict | str) -> AsyncIterator[str]:
    for event in events:
        yield event if isinstance(event, str) else json.dumps(event)


async def _drain(stream: AsyncIterator[dict]) -> list[dict]:
    return [event async for event in stream]


def _delta(**delta) -> dict:
    return {"choices": [{"delta": delta}]}


def _openai(acc: ChatStream, *events: dict | str, forced: bool = False, aborted: bool = False) -> AsyncIterator[dict]:
    return consume_openai(
        _payloads(*events),
        acc,
        forced=forced,
        url="https://api.example/v1/chat/completions",
        model="m",
        api_key="",
        is_aborted=lambda: aborted,
    )


async def test_openai_streams_content_and_records_the_reasoning_field_name():
    acc = ChatStream()
    events = await _drain(
        _openai(
            acc,
            _delta(reasoning="Think."),
            _delta(content="Hel"),
            _delta(content="lo"),
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3}},
        )
    )
    assert events == [
        {"type": "reasoning", "delta": "Think."},
        {"type": "content", "delta": "Hel"},
        {"type": "content", "delta": "lo"},
    ]
    assert acc.reasoning_key == "reasoning"
    assert "".join(acc.content_parts) == "Hello"
    assert acc.finish_reason == "stop"
    assert acc.usage == {"prompt_tokens": 3}


async def test_openai_forced_call_buffers_content_instead_of_streaming_it():
    acc = ChatStream()
    events = await _drain(_openai(acc, _delta(content='{"a":'), _delta(content="1}"), "[DONE]", forced=True))
    assert events == []
    assert "".join(acc.content_parts) == '{"a":1}'


async def test_openai_index_less_tool_calls_open_one_slot_per_call():
    acc = ChatStream()
    await _drain(
        _openai(
            acc,
            _delta(tool_calls=[{"id": "a", "function": {"name": "first", "arguments": '{"x":'}}]),
            _delta(tool_calls=[{"function": {"arguments": "1}"}}]),
            _delta(tool_calls=[{"id": "b", "function": {"name": "second", "arguments": "{}"}}]),
            "[DONE]",
        )
    )
    assert [(c["id"], c["function"]["name"], c["function"]["arguments"]) for c in acc.tool_calls.values()] == [
        ("a", "first", '{"x":1}'),
        ("b", "second", "{}"),
    ]


async def test_openai_in_band_error_raises_a_provider_error():
    with pytest.raises(LLMCallError):
        await _drain(_openai(ChatStream(), {"error": {"message": "overloaded"}}, "[DONE]"))


async def test_openai_truncated_stream_raises_unless_the_turn_was_stopped():
    partial = _delta(content="partial")
    with pytest.raises(LLMCallError):
        await _drain(_openai(ChatStream(), partial))
    assert await _drain(_openai(ChatStream(), partial, aborted=True)) == [{"type": "content", "delta": "partial"}]


def _anthropic(acc: ChatStream, *events: dict, aborted: bool = False) -> AsyncIterator[dict]:
    return anthropic.consume_stream(
        _payloads(*events),
        acc,
        forced=False,
        url="https://api.example/v1/messages",
        model="m",
        api_key="",
        is_aborted=lambda: aborted,
    )


async def test_anthropic_maps_blocks_and_stop_reason_to_openai_terms():
    acc = ChatStream()
    events = await _drain(
        _anthropic(
            acc,
            {"type": "message_start", "message": {"usage": {"input_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Hmm."}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t", "name": "f"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        )
    )
    assert events == [{"type": "reasoning", "delta": "Hmm."}]
    assert acc.tool_calls[1]["function"] == {"name": "f", "arguments": "{}"}
    assert acc.finish_reason == "tool_calls"
    assert acc.usage == {"input_tokens": 5, "output_tokens": 2}


async def test_anthropic_error_event_raises_a_provider_error():
    with pytest.raises(LLMCallError):
        await _drain(_anthropic(ChatStream(), {"type": "error", "error": {"message": "overloaded"}}))


async def test_anthropic_truncated_stream_raises_unless_the_turn_was_stopped():
    start = {"type": "message_start", "message": {}}
    with pytest.raises(LLMCallError):
        await _drain(_anthropic(ChatStream(), start))
    assert await _drain(_anthropic(ChatStream(), start, aborted=True)) == []
