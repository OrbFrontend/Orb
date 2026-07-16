"""Structured forced calls: profile gating, strict-schema massage, chat rewrite."""

from __future__ import annotations

import json
from unittest.mock import patch

import backend.inference.client as llm_mod
from backend.inference.client import LLMClient, parse_tool_calls, strictify_schema
from backend.inference.endpoint_profiles import supports_structured_tool_calls

# ── strictify_schema ──────────────────────────────────────────────────────────


def test_strictify_requires_all_and_closes_object():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    out = strictify_schema(schema)
    assert out["required"] == ["a", "b"]
    assert out["additionalProperties"] is False
    assert out["properties"]["a"] == {"type": "string"}


def test_strictify_makes_optionals_nullable():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "opt": {"type": "string"}},
        "required": ["a"],
    }
    out = strictify_schema(schema)
    assert out["properties"]["opt"]["type"] == ["string", "null"]
    assert out["properties"]["a"]["type"] == "string"
    assert set(out["required"]) == {"a", "opt"}


def test_strictify_recurses_into_array_items():
    schema = {
        "type": "object",
        "properties": {
            "patches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"search": {"type": "string"}, "replace": {"type": "string"}},
                    "required": ["search", "replace"],
                },
            }
        },
        "required": ["patches"],
    }
    out = strictify_schema(schema)
    items = out["properties"]["patches"]["items"]
    assert items["additionalProperties"] is False
    assert set(items["required"]) == {"search", "replace"}


def test_strictify_leaves_original_untouched():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    strictify_schema(schema)
    assert "additionalProperties" not in schema
    assert schema["properties"]["a"] == {"type": "string"}


# ── profile gating ────────────────────────────────────────────────────────────


def test_gating_by_endpoint():
    assert supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")
    assert supports_structured_tool_calls("https://NANO-GPT.com/api/v1")  # case-insensitive
    assert not supports_structured_tool_calls("https://api.deepseek.com/v1", "deepseek-chat")
    assert not supports_structured_tool_calls("http://localhost:5000/v1")
    assert not supports_structured_tool_calls("")


# ── wire-level: the chat transport rewrite ────────────────────────────────────


DIRECT_SCENE = {
    "type": "function",
    "function": {
        "name": "direct_scene",
        "description": "Direct the scene.",
        "parameters": {
            "type": "object",
            "properties": {"history-summary": {"type": "string"}, "moods": {"type": "array", "items": {"type": "string"}}},
            "required": ["history-summary"],
        },
    },
}

FORCED = {"type": "function", "function": {"name": "direct_scene"}}

ARGS_JSON = '{"history-summary": "so far", "moods": ["eerie"]}'


class _FakeStream:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeAsyncClient:
    def __init__(self, lines):
        self._lines = lines
        self.bodies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.bodies.append(dict(json or {}))
        return _FakeStream(self._lines)


def _content_lines(text: str) -> list[str]:
    return [
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
        f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]})}",
        "data: [DONE]",
    ]


async def _run(client: LLMClient, lines, **kwargs):
    fake = _FakeAsyncClient(lines)
    events = []
    with patch.object(llm_mod.httpx, "AsyncClient", lambda *a, **k: fake):
        async for ev in client.complete([], "TEE/glm-5.2:thinking", **kwargs):
            events.append(ev)
    assert len(fake.bodies) == 1
    return fake.bodies[0], events


async def test_forced_call_rewritten_as_structured_output():
    client = LLMClient("https://nano-gpt.com/api/v1")
    body, events = await _run(client, _content_lines(ARGS_JSON), tools=[DIRECT_SCENE], tool_choice=FORCED)

    assert "tool_choice" not in body
    assert body["tools"] == [DIRECT_SCENE]  # prompt/KV stability: tools stay
    rf = body["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "direct_scene"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False
    assert "history-summary" in rf["json_schema"]["schema"]["properties"]

    # Arguments never leak as content deltas; reasoning still streams.
    assert not [e for e in events if e["type"] == "content"]
    assert [e for e in events if e["type"] == "reasoning"]

    message = events[-1]["message"]
    calls = parse_tool_calls(message)
    assert calls == [{"name": "direct_scene", "arguments": {"history-summary": "so far", "moods": ["eerie"]}}]


async def test_json_schema_override_narrows_structured_schema():
    client = LLMClient("https://nano-gpt.com/api/v1")
    narrow = {"type": "object", "properties": {"moods": {"type": "array", "items": {"type": "string"}}}, "required": ["moods"]}
    body, _ = await _run(client, _content_lines('{"moods": []}'), tools=[DIRECT_SCENE], tool_choice=FORCED, json_schema=narrow)
    props = body["response_format"]["json_schema"]["schema"]["properties"]
    assert list(props) == ["moods"]


async def test_unknown_endpoint_keeps_forced_tool_choice():
    client = LLMClient("http://localhost:5000/v1")
    body, _ = await _run(client, _content_lines("hi"), tools=[DIRECT_SCENE], tool_choice=FORCED, json_schema={"type": "object"})
    assert body["tool_choice"] == FORCED
    assert "response_format" not in body
    assert "json_schema" not in body  # consumed by the transport, never sent raw


async def test_auto_choice_not_rewritten():
    client = LLMClient("https://nano-gpt.com/api/v1")
    body, events = await _run(client, _content_lines("plain prose"), tools=[DIRECT_SCENE], tool_choice="auto")
    assert body["tool_choice"] == "auto"
    assert "response_format" not in body
    assert [e for e in events if e["type"] == "content"]  # prose streams normally


async def test_forced_without_schema_falls_through():
    client = LLMClient("https://nano-gpt.com/api/v1")
    unknown_forced = {"type": "function", "function": {"name": "not_in_tools"}}
    body, _ = await _run(client, _content_lines("hi"), tools=[DIRECT_SCENE], tool_choice=unknown_forced)
    assert body["tool_choice"] == unknown_forced
    assert "response_format" not in body


async def test_real_tool_calls_win_over_synthesis():
    # A provider that answers a structured request with genuine tool_calls
    # anyway: prefer them over content synthesis.
    client = LLMClient("https://nano-gpt.com/api/v1")
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function","function":{"name":"direct_scene","arguments":"{}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    _, events = await _run(client, lines, tools=[DIRECT_SCENE], tool_choice=FORCED)
    message = events[-1]["message"]
    assert message["tool_calls"][0]["id"] == "c1"


async def test_unparseable_content_degrades_to_empty_args():
    client = LLMClient("https://nano-gpt.com/api/v1")
    _, events = await _run(client, _content_lines("not json at all"), tools=[DIRECT_SCENE], tool_choice=FORCED)
    calls = parse_tool_calls(events[-1]["message"])
    assert calls == [{"name": "direct_scene", "arguments": {}}]
