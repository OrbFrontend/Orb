"""Structured forced calls: profile gating, strict-schema massage, chat rewrite."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import backend.inference.client as llm_mod
import backend.inference.endpoint_profiles as ep_mod
from backend.inference.client import LLMClient, parse_tool_calls
from backend.inference.endpoint_profiles import supports_structured_tool_calls
from backend.inference.schema import strictify_schema


@pytest.fixture(autouse=True)
def _no_learned_demotions():
    """The demotion set is process-global; keep it from leaking between tests."""
    ep_mod._STRUCTURED_OUTPUT_IGNORED.clear()
    yield
    ep_mod._STRUCTURED_OUTPUT_IGNORED.clear()


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

_IMAGE_CONTENT = [
    {"type": "text", "text": "hi"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
]


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


class _ReplayAsyncClient(_FakeAsyncClient):
    """Serves a different SSE script per request, repeating the last one."""

    def __init__(self, scripts):
        super().__init__(scripts[0])
        self._scripts = scripts

    def stream(self, method, url, json=None, headers=None):
        self.bodies.append(dict(json or {}))
        return _FakeStream(self._scripts[min(len(self.bodies) - 1, len(self._scripts) - 1)])


def _content_lines(text: str) -> list[str]:
    return [
        'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
        f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]})}",
        "data: [DONE]",
    ]


async def _run(client: LLMClient, lines, *, messages=None, **kwargs):
    bodies, events = await _run_raw(client, lines, messages=messages, **kwargs)
    assert len(bodies) == 1
    return bodies[0], events


async def _run_raw(client: LLMClient, lines, *, messages=None, **kwargs):
    """Every request the call made, in order. *lines* may be a list per request."""
    per_request = lines if lines and isinstance(lines[0], list) else [lines]
    fake = _ReplayAsyncClient(list(per_request))
    events = []
    with patch.object(llm_mod.httpx, "AsyncClient", lambda *a, **k: fake):
        async for ev in client.complete(messages if messages is not None else [], "TEE/glm-5.2:thinking", **kwargs):
            events.append(ev)
    return fake.bodies, events


async def test_forced_call_rewritten_as_structured_output():
    client = LLMClient("https://nano-gpt.com/api/v1")
    body, events = await _run(client, _content_lines(ARGS_JSON), tools=[DIRECT_SCENE], tool_choice=FORCED)

    assert "tool_choice" not in body
    # The schema is derived from `tools`, but `tools` itself is withheld: left
    # in, the model can answer with a native tool call that bypasses the schema.
    assert "tools" not in body
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


async def test_tools_in_prompt_false_forces_structured_without_tools():
    # No profile (localhost llama.cpp) — the flag alone triggers the rewrite,
    # and the tools blob is dropped: the caller's conversation has no schemas
    # in its cached prefix, so none may enter the server-rendered prompt.
    client = LLMClient("http://localhost:5000/v1")
    body, events = await _run(
        client, _content_lines(ARGS_JSON), tools=[DIRECT_SCENE], tool_choice=FORCED, tools_in_prompt=False
    )
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "tools_in_prompt" not in body  # transport flag, never wire bytes
    rf = body["response_format"]
    assert rf["json_schema"]["name"] == "direct_scene"
    assert rf["json_schema"]["strict"] is True
    # Content buffers as arguments and re-synthesizes as a tool call.
    assert not [e for e in events if e["type"] == "content"]
    calls = parse_tool_calls(events[-1]["message"])
    assert calls == [{"name": "direct_scene", "arguments": {"history-summary": "so far", "moods": ["eerie"]}}]


async def test_tools_in_prompt_false_non_forced_drops_tools_and_choice():
    client = LLMClient("http://localhost:5000/v1")
    body, events = await _run(
        client, _content_lines("plain prose"), tools=[DIRECT_SCENE], tool_choice="auto", tools_in_prompt=False
    )
    assert "tools" not in body and "tool_choice" not in body
    assert "response_format" not in body
    assert [e for e in events if e["type"] == "content"]  # prose streams normally


async def test_auto_choice_not_rewritten():
    client = LLMClient("https://nano-gpt.com/api/v1")
    body, events = await _run(client, _content_lines("plain prose"), tools=[DIRECT_SCENE], tool_choice="auto")
    assert "response_format" not in body  # only a forced call becomes structured
    assert [e for e in events if e["type"] == "content"]  # prose streams normally


async def test_structured_endpoint_omits_tools_on_every_pass():
    """The tool blob is withheld for unforced passes too, not just forced ones.

    This is what keeps one stable prefix. The server renders `tools` into the
    prompt, so sending it on the writer's pass and not the director's would
    hand the two different prefixes and thrash the KV base they share.
    """
    client = LLMClient("https://nano-gpt.com/api/v1")
    for choice in (FORCED, "auto", "none", "required", None):
        body, _ = await _run(client, _content_lines(ARGS_JSON), tools=[DIRECT_SCENE], tool_choice=choice)
        assert "tools" not in body, choice
        assert "tool_choice" not in body, choice


async def test_unlisted_endpoint_still_sends_tools():
    # The omission is scoped to endpoints that take structured output; everyone
    # else keeps ordinary tool calling.
    client = LLMClient("http://localhost:5000/v1")
    body, _ = await _run(client, _content_lines("hi"), tools=[DIRECT_SCENE], tool_choice="auto")
    assert body["tools"] == [DIRECT_SCENE]
    assert body["tool_choice"] == "auto"


async def test_forced_without_schema_degrades_to_plain_completion():
    # The forced name is not in `tools`, so no schema can be built. Rather than
    # send a tool_choice pointing at a tool the body no longer carries, the call
    # goes out unconstrained and the parse_tool_calls recovery chain handles the
    # reply -- the same posture as any unforced pass.
    client = LLMClient("https://nano-gpt.com/api/v1")
    unknown_forced = {"type": "function", "function": {"name": "not_in_tools"}}
    body, _ = await _run(client, _content_lines("hi"), tools=[DIRECT_SCENE], tool_choice=unknown_forced)
    assert "response_format" not in body
    assert "tools" not in body
    assert "tool_choice" not in body


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
    # tools_in_prompt=False has no second shape to retry into (its prefix must
    # stay schema-free), so this is the path where the degrade still stands
    # alone: unparseable forced content becomes a call with no arguments.
    client = LLMClient("https://nano-gpt.com/api/v1")
    _, events = await _run(
        client, _content_lines("not json at all"), tools=[DIRECT_SCENE], tool_choice=FORCED, tools_in_prompt=False
    )
    calls = parse_tool_calls(events[-1]["message"])
    assert calls == [{"name": "direct_scene", "arguments": {}}]


# ── learned demotion: a route that accepts the schema and ignores it ──────────


async def test_non_json_reply_demotes_the_pair_and_restores_tool_calling():
    """The reply is the only evidence: a 200 that ignores the schema.

    NanoGPT fronts a different upstream engine per model, so the profile opt-in
    is optimistic. One completed non-object reply proves this route decodes
    unconstrained, and the next call must go back to ordinary tool calling
    rather than keep feeding a constraint nobody applies.
    """
    client = LLMClient("https://nano-gpt.com/api/v1")
    memo = '<memo lang="en"><small>- moods: [talkative, grounded]</small></memo>'

    bodies, _ = await _run_raw(client, _content_lines(memo), tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert "response_format" in bodies[0] and "tools" not in bodies[0]  # the attempt that taught us
    assert ("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking") in ep_mod._STRUCTURED_OUTPUT_IGNORED
    assert not supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")

    body, _ = await _run(client, _content_lines(ARGS_JSON), tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert "response_format" not in body
    assert body["tools"] == [DIRECT_SCENE]
    assert body["tool_choice"] == FORCED
    # The writer's prompt guard has to move with it, or it nudges against a
    # schema block the request no longer omits.
    assert client.sends_tool_schemas([], "TEE/glm-5.2:thinking")


async def test_the_demoting_call_retries_and_keeps_its_own_turn():
    """The reply that teaches us is not also a lost pass.

    A forced call buffers its arguments instead of streaming them, so at the
    moment the schema is disproved nothing but reasoning has reached the
    caller -- the request can simply be re-issued in the shape that works.
    """
    client = LLMClient("https://nano-gpt.com/api/v1")
    native = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","type":"function",'
        '"function":{"name":"direct_scene","arguments":' + json.dumps(ARGS_JSON) + "}}]},"
        '"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    bodies, events = await _run_raw(
        client, [_content_lines("prose, not the object"), native], tools=[DIRECT_SCENE], tool_choice=FORCED
    )

    assert len(bodies) == 2
    assert "response_format" in bodies[0] and "tools" not in bodies[0]
    assert "response_format" not in bodies[1]
    assert bodies[1]["tools"] == [DIRECT_SCENE] and bodies[1]["tool_choice"] == FORCED
    # Only the retry's reply survives; the discarded attempt leaves nothing behind.
    assert parse_tool_calls(events[-1]["message"]) == [
        {"name": "direct_scene", "arguments": {"history-summary": "so far", "moods": ["eerie"]}}
    ]
    assert events[-1]["message"]["tool_calls"][0]["id"] == "c1"


async def test_a_retry_that_also_fails_degrades_instead_of_looping():
    client = LLMClient("https://nano-gpt.com/api/v1")
    bodies, events = await _run_raw(client, _content_lines("prose again"), tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert len(bodies) == 2
    # Unforced now, so the reply is ordinary content and the recovery chain
    # finds no call -- the "model skipped this tool" shape passes already handle.
    assert parse_tool_calls(events[-1]["message"]) == []


async def test_demotion_is_scoped_to_the_model_that_proved_it():
    client = LLMClient("https://nano-gpt.com/api/v1")
    await _run_raw(client, _content_lines("prose, not the object"), tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert not supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")
    assert supports_structured_tool_calls("https://nano-gpt.com/api/v1", "some/other-model")


async def test_truncated_and_empty_replies_are_not_evidence():
    """Neither proves the constraint was absent, and demoting costs the good lane."""
    client = LLMClient("https://nano-gpt.com/api/v1")

    cut_off = [
        f"data: {json.dumps({'choices': [{'delta': {'content': ARGS_JSON[:20]}, 'finish_reason': 'length'}]})}",
        "data: [DONE]",
    ]
    await _run(client, cut_off, tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")

    await _run(client, _content_lines(""), tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")


async def test_a_scalar_reply_is_evidence_too():
    # Valid JSON, but no schema on this path describes anything but an object.
    client = LLMClient("https://nano-gpt.com/api/v1")
    await _run_raw(client, _content_lines('"just a string"'), tools=[DIRECT_SCENE], tool_choice=FORCED)
    assert not supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")


async def test_tools_in_prompt_false_never_gains_schemas_from_a_demotion():
    """The doc-mode auditor's contract is prompt bytes, not an optimization.

    Its prefix has no schemas, so no reply may ever put them there -- the
    demotion is only allowed to revoke the profile's opt-in.
    """
    client = LLMClient("https://nano-gpt.com/api/v1")
    await _run(client, _content_lines("not the object"), tools=[DIRECT_SCENE], tool_choice=FORCED, tools_in_prompt=False)
    body, _ = await _run(client, _content_lines(ARGS_JSON), tools=[DIRECT_SCENE], tool_choice=FORCED, tools_in_prompt=False)
    assert "tools" not in body and "tool_choice" not in body
    assert body["response_format"]["json_schema"]["name"] == "direct_scene"


# ── sends_tool_schemas: the wire-level predicate used by model lanes ──────────


async def test_sends_tool_schemas_agrees_with_the_wire():
    """The predicate must match what `_complete_chat` puts in the body.

    Derived independently, so pin them together: a drift means the writer's
    prompt guard disagrees with the schema-delivery behavior Orb controls.
    """
    for url in ("https://nano-gpt.com/api/v1", "http://localhost:5000/v1"):
        client = LLMClient(url)
        body, _ = await _run(client, _content_lines("hi"), tools=[DIRECT_SCENE], tool_choice="none")
        assert client.sends_tool_schemas([], "TEE/glm-5.2:thinking") is ("tools" in body), url


async def test_sends_tool_schemas_tracks_text_modes_multimodal_chat_branch():
    messages = [{"role": "user", "content": _IMAGE_CONTENT}]
    client = LLMClient("http://localhost:5000/v1", completion_mode="text")
    assert not client.sends_tool_schemas([], "TEE/glm-5.2:thinking")
    assert client.sends_tool_schemas(messages, "TEE/glm-5.2:thinking")

    body, _ = await _run(
        client,
        _content_lines("hi"),
        messages=messages,
        tools=[DIRECT_SCENE],
        tool_choice="none",
    )
    assert "tools" in body


async def test_a_stopped_call_is_not_evidence():
    """A user stop truncates the reply mid-decode; that proves nothing.

    Reading it as proof would demote a perfectly good endpoint every time
    someone hits stop, and burn a retry on a turn already being discarded.
    """
    client = LLMClient("https://nano-gpt.com/api/v1")

    class _AbortMidStream(_FakeStream):
        async def aiter_lines(self):
            for ln in self._lines:
                yield ln
                client.abort()

    fake = _ReplayAsyncClient([_content_lines("half an ans")])
    fake.stream = lambda m, u, json=None, headers=None: (  # type: ignore[method-assign]
        fake.bodies.append(dict(json or {})),
        _AbortMidStream(_content_lines("half an ans")),
    )[1]

    with patch.object(llm_mod.httpx, "AsyncClient", lambda *a, **k: fake):
        async for _ in client.complete([], "TEE/glm-5.2:thinking", tools=[DIRECT_SCENE], tool_choice=FORCED):
            pass

    assert len(fake.bodies) == 1  # no retry
    assert supports_structured_tool_calls("https://nano-gpt.com/api/v1", "TEE/glm-5.2:thinking")
