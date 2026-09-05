"""Protocol resolution, Anthropic adaptation/streaming, and bounded probing."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.inference import anthropic
from backend.inference import client as llm_mod
from backend.inference import endpoint_profiles as ep
from backend.inference.client import LLMClient, parse_tool_calls, reasoning_cfg
from backend.inference.errors import LLMCallError

TOOL = {
    "type": "function",
    "function": {
        "name": "direct_scene",
        "description": "Direct it",
        "parameters": {
            "type": "object",
            "properties": {"mood": {"type": "string"}},
            "required": ["mood"],
        },
    },
}
FORCED = {"type": "function", "function": {"name": "direct_scene"}}


@pytest.fixture(autouse=True)
def _clear_learned_state():
    ep._RESOLVED_ROUTES.clear()
    ep._TOOL_CHOICE_AUTO_ONLY.clear()
    ep._TOOL_CHOICE_UNSUPPORTED.clear()
    anthropic._SAMPLING_UNSUPPORTED.clear()
    yield
    ep._RESOLVED_ROUTES.clear()
    ep._TOOL_CHOICE_AUTO_ONLY.clear()
    ep._TOOL_CHOICE_UNSUPPORTED.clear()
    anthropic._SAMPLING_UNSUPPORTED.clear()


@pytest.mark.parametrize(
    ("configured", "protocol", "url", "models"),
    [
        (
            "https://openai.test/v1/chat/completions",
            "openai",
            "https://openai.test/v1/chat/completions",
            "https://openai.test/v1/models",
        ),
        (
            "https://proxy.test/prefix/v1/messages",
            "anthropic",
            "https://proxy.test/prefix/v1/messages",
            "https://proxy.test/prefix/v1/models",
        ),
        (
            "https://api.anthropic.com",
            "anthropic",
            "https://api.anthropic.com/v1/messages",
            "https://api.anthropic.com/v1/models",
        ),
        (
            "https://proxy.test/providers/anthropic/v1",
            "anthropic",
            "https://proxy.test/providers/anthropic/v1/messages",
            "https://proxy.test/providers/anthropic/v1/models",
        ),
        (
            "https://generativelanguage.googleapis.com/v99/wrong",
            "openai",
            "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "https://generativelanguage.googleapis.com/v1beta/openai/models",
        ),
    ],
)
def test_deterministic_resolution(configured, protocol, url, models):
    route = ep.resolve_endpoint(configured)
    assert route.protocol == protocol
    assert route.url == url
    assert route.models_url == models
    assert route.authoritative


def test_explicit_resource_is_authoritative_even_on_provider_host():
    route = ep.resolve_endpoint("https://api.anthropic.com/v1/chat/completions")
    assert route.protocol == "openai"
    assert route.url.endswith("/v1/chat/completions")


def test_ambiguous_candidates_preserve_old_request_then_same_host_v1():
    routes = ep.endpoint_candidates("https://custom.test/prefix", "m")
    assert [(route.protocol, route.url) for route in routes] == [
        ("openai", "https://custom.test/prefix/chat/completions"),
        ("openai", "https://custom.test/v1/chat/completions"),
        ("anthropic", "https://custom.test/v1/messages"),
    ]


def test_malformed_url_degrades_without_probe_candidates():
    routes = ep.endpoint_candidates("not a url", "m")
    assert len(routes) == 1
    assert routes[0].protocol == "openai"


def test_route_probe_is_body_based():
    assert ep.should_probe_route(400, '{"error":"Unknown endpoint"}')
    assert ep.should_probe_route(404, "Cannot POST /chat/completions")
    assert not ep.should_probe_route(404, '{"error":"model not found"}')
    assert not ep.should_probe_route(400, '{"error":"Unknown model"}')


def test_tool_schema_predicate_tracks_anthropic_and_gemini_wire_shapes():
    messages = [{"role": "user", "content": "hi"}]
    assert LLMClient("https://api.anthropic.com").sends_tool_schemas(messages, "claude-opus-5")
    assert not LLMClient("https://generativelanguage.googleapis.com").sends_tool_schemas(messages, "gemini-3-pro")


def test_translate_messages_system_images_tools_and_coalescing():
    messages = [
        {"role": "system", "content": "one"},
        {"role": "system", "content": [{"type": "text", "text": "two"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
            ],
        },
        {
            "role": "assistant",
            "content": "calling",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "direct_scene", "arguments": '{"mood":"eerie"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "done"},
        {"role": "user", "content": "continue"},
    ]
    system, out = anthropic.translate_messages(messages)
    assert system == "one\n\ntwo"
    assert out[0]["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "eA=="},
    }
    assert out[1]["content"][1] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "direct_scene",
        "input": {"mood": "eerie"},
    }
    # tool result and following user content become one legal adjacent user turn.
    assert out[2]["role"] == "user"
    assert [block["type"] for block in out[2]["content"]] == ["tool_result", "text"]


def test_anthropic_body_allowlist_tools_choices_reasoning_and_sampling():
    canonical = {
        "model": "claude-haiku-4-5",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "tools": [TOOL],
        "tool_choice": FORCED,
        "temperature": 0.8,
        "top_p": 0.95,
        "top_k": 40,
        "min_p": 0.1,
        "repetition_penalty": 1.1,
        "reasoning": {"enabled": True},
        "reasoning_effort": "xhigh",
        "chat_template_kwargs": {"thinking": True},
        "stream_options": {"include_usage": True},
        "logprobs": True,
    }
    body = anthropic.build_request_body(
        canonical,
        "https://api.anthropic.com/v1/messages",
        "claude-haiku-4-5",
        {"metadata": {"user_id": "u"}, "response_format": {"type": "json"}, "seed": 7},
    )
    assert body["max_tokens"] == anthropic.DEFAULT_MAX_TOKENS
    assert body["tools"][0]["strict"] is True
    assert body["tools"][0]["input_schema"] == TOOL["function"]["parameters"]
    assert body["tool_choice"] == {"type": "tool", "name": "direct_scene"}
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "xhigh"}
    assert body["temperature"] == 0.8 and body["top_p"] == 0.95 and body["top_k"] == 40
    assert body["metadata"] == {"user_id": "u"}
    for forbidden in (
        "reasoning",
        "chat_template_kwargs",
        "stream_options",
        "logprobs",
        "min_p",
        "repetition_penalty",
        "response_format",
        "seed",
    ):
        assert forbidden not in body


def test_current_claude_sampling_is_withheld_and_reasoning_off_omits_thinking():
    body = anthropic.build_request_body(
        {
            "messages": [],
            "temperature": 0.8,
            "top_p": 0.95,
            "top_k": 40,
            "thinking": {"type": "disabled"},
        },
        "https://api.anthropic.com",
        "claude-opus-5-20260801",
    )
    assert "temperature" not in body and "top_p" not in body and "top_k" not in body
    assert "thinking" not in body and "output_config" not in body


def test_tool_choice_mapping():
    assert anthropic.translate_tool_choice("none") == {"type": "none"}
    assert anthropic.translate_tool_choice("auto") == {"type": "auto"}
    assert anthropic.translate_tool_choice("required") == {"type": "any"}
    assert anthropic.translate_tool_choice(FORCED) == {"type": "tool", "name": "direct_scene"}


class _Response:
    def __init__(self, status=200, *, error="", lines=()):
        self.status_code = status
        self.error = error
        self.lines = list(lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aread(self):
        return self.error.encode()

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _HTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.requests.append({"method": method, "url": url, "body": dict(json or {}), "headers": dict(headers or {})})
        return self.responses.pop(0)


def _line(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


ANTHROPIC_TOOL_STREAM = [
    _line({"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 4}}}),
    _line({"type": "ping"}),
    _line({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "why"}}),
    _line({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": " now"}}),
    _line(
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tool-1", "name": "direct_scene", "input": {}},
        }
    ),
    _line({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"mood"'}}),
    _line({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ':"eerie"}'}}),
    _line({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}}),
    _line({"type": "message_stop"}),
    _line({"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "ignored"}}),
]


async def _run(client: LLMClient, fake: _HTTP, model="claude-haiku-4-5", **kwargs):
    events = []
    with patch.object(llm_mod.httpx, "AsyncClient", lambda *args, **kw: fake):
        async for event in client.complete([{"role": "user", "content": "hi"}], model, **kwargs):
            events.append(event)
    return events


async def test_anthropic_wire_headers_body_and_stream_translation():
    fake = _HTTP([_Response(lines=ANTHROPIC_TOOL_STREAM)])
    client = LLMClient(
        "https://api.anthropic.com/v1/messages",
        "sk-test",
        reasoning_effort="high",
        extra_headers="X-Custom: yes\nanthropic-version: 2026-01-01",
        extra_body='{"seed": 9}',
    )
    events = await _run(
        client,
        fake,
        tools=[TOOL],
        tool_choice=FORCED,
        max_tokens=123,
        temperature=0.4,
        **reasoning_cfg(True),
    )
    request = fake.requests[0]
    assert request["url"] == "https://api.anthropic.com/v1/messages"
    assert request["headers"] == {"x-api-key": "sk-test", "X-Custom": "yes", "anthropic-version": "2026-01-01"}
    assert "Authorization" not in request["headers"]
    assert request["body"]["max_tokens"] == 123
    assert request["body"]["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request["body"]["tool_choice"] == {"type": "tool", "name": "direct_scene"}
    assert "seed" not in request["body"]
    assert [event["delta"] for event in events if event["type"] == "reasoning"] == ["why", " now"]
    assert not [event for event in events if event["type"] == "content"]
    done = events[-1]
    assert done["usage"] == {"input_tokens": 10, "cache_read_input_tokens": 4, "output_tokens": 7}
    assert done["message"]["finish_reason"] == "tool_calls"
    assert parse_tool_calls(done["message"]) == [{"name": "direct_scene", "arguments": {"mood": "eerie"}}]


async def test_anthropic_message_stop_terminates_without_done_sentinel():
    lines = [
        _line({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hello"}}),
        _line({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        _line({"type": "message_stop"}),
    ]
    events = await _run(LLMClient("https://api.anthropic.com/v1/messages"), _HTTP([_Response(lines=lines)]))
    assert events[0] == {"type": "content", "delta": "hello"}
    assert events[-1]["message"] == {"content": "hello", "finish_reason": "stop"}


async def test_gemini_uses_normalized_openai_route_structured_output_and_effort():
    lines = [
        'data: {"choices":[{"delta":{"content":"{\\"mood\\":\\"bright\\"}"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    fake = _HTTP([_Response(lines=lines)])
    client = LLMClient("https://generativelanguage.googleapis.com", "key", reasoning_effort="high")
    events = await _run(
        client,
        fake,
        model="gemini-3-pro",
        tools=[TOOL],
        tool_choice=FORCED,
        **reasoning_cfg(True),
    )
    request = fake.requests[0]
    assert request["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    assert request["headers"] == {"Authorization": "Bearer key"}
    assert "tools" not in request["body"] and "tool_choice" not in request["body"]
    assert request["body"]["reasoning_effort"] == "high"
    assert request["body"]["response_format"]["json_schema"]["strict"] is True
    assert parse_tool_calls(events[-1]["message"]) == [{"name": "direct_scene", "arguments": {"mood": "bright"}}]


async def test_anthropic_midstream_error_uses_sanitized_llm_error():
    lines = [
        _line({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}}),
        _line({"type": "error", "error": {"type": "overloaded_error", "message": "busy sk-secret"}}),
    ]
    with pytest.raises(LLMCallError) as caught:
        await _run(LLMClient("https://api.anthropic.com/v1/messages", "sk-secret"), _HTTP([_Response(lines=lines)]))
    assert caught.value.response.status_code == 502
    assert caught.value.sentence == "busy [redacted]"
    assert "sk-secret" not in caught.value.body


async def test_ambiguous_endpoint_probes_bounded_routes_and_caches_success():
    wrong = '{"error":"Cannot POST /prefix/chat/completions"}'
    wrong_v1 = '{"error":"route not found"}'
    success = [
        _line({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}}),
        _line({"type": "message_stop"}),
    ]
    fake = _HTTP([_Response(404, error=wrong), _Response(404, error=wrong_v1), _Response(lines=success)])
    client = LLMClient("https://custom.test/prefix")
    events = await _run(client, fake, model="claude-proxy")
    assert [request["url"] for request in fake.requests] == [
        "https://custom.test/prefix/chat/completions",
        "https://custom.test/v1/chat/completions",
        "https://custom.test/v1/messages",
    ]
    assert events[-1]["message"]["content"] == "hi"
    assert ep.resolve_endpoint(client.base_url, "claude-proxy").protocol == "anthropic"

    again = _HTTP([_Response(lines=success)])
    await _run(client, again, model="claude-proxy")
    assert [request["url"] for request in again.requests] == ["https://custom.test/v1/messages"]


async def test_auth_family_retry_is_evidence_gated_and_bounded():
    success = [_line({"type": "message_stop"})]
    fake = _HTTP([_Response(401, error='{"error":"bad auth"}'), _Response(lines=success)])
    await _run(LLMClient("https://proxy.test/v1/messages", "key"), fake, model="claude-proxy")
    assert fake.requests[0]["headers"]["x-api-key"] == "key"
    assert fake.requests[1]["headers"]["Authorization"] == "Bearer key"
    assert "x-api-key" not in fake.requests[1]["headers"]


async def test_openai_auth_rejection_without_claude_evidence_is_not_retried():
    fake = _HTTP([_Response(401, error='{"error":"bad auth"}')])
    with pytest.raises(LLMCallError):
        await _run(LLMClient("https://custom.test/v1", "key"), fake, model="ordinary-model")
    assert len(fake.requests) == 1


async def test_sampling_rejection_is_learned_and_retried_once():
    rejection = '{"error":{"message":"temperature is not supported"}}'
    fake = _HTTP([_Response(400, error=rejection), _Response(lines=[_line({"type": "message_stop"})])])
    client = LLMClient("https://proxy.test/v1/messages")
    await _run(client, fake, model="hidden-model", temperature=0.8, top_p=0.9, top_k=20)
    assert fake.requests[0]["body"]["temperature"] == 0.8
    assert not {"temperature", "top_p", "top_k"} & fake.requests[1]["body"].keys()
    assert (client.base_url, "hidden-model") in anthropic._SAMPLING_UNSUPPORTED


async def test_anthropic_forced_choice_rejection_falls_back_to_auto():
    rejection = '{"error":{"message":"tool_choice type any is not supported for this model"}}'
    fake = _HTTP(
        [
            _Response(400, error=rejection),
            _Response(lines=[_line({"type": "message_stop"})]),
        ]
    )
    client = LLMClient("https://proxy.test/v1/messages")
    await _run(client, fake, model="claude-fable-5-1", tools=[TOOL], tool_choice="required")
    assert fake.requests[0]["body"]["tool_choice"] == {"type": "any"}
    assert fake.requests[1]["body"]["tool_choice"] == {"type": "auto"}
    assert not ep.honors_forced_tool_choice(client.base_url, "claude-fable-5-1")


@pytest.mark.parametrize("choice", ["none", "required", FORCED])
async def test_auto_only_tool_choice_recovery_and_process_memory(choice):
    rejection = '{"error":{"message":"only \\"auto\\" is supported for tool_choice"}}'
    openai_done = ['data: {"choices":[{"delta":{},"finish_reason":"stop"}]}', "data: [DONE]"]
    fake = _HTTP([_Response(400, error=rejection), _Response(lines=openai_done)])
    client = LLMClient("https://openrouter.ai/api/v1")
    await _run(client, fake, model="unlisted/model", tools=[TOOL], tool_choice=choice)
    assert fake.requests[1]["body"]["tool_choice"] == "auto"
    assert (client.base_url, "unlisted/model") in ep._TOOL_CHOICE_AUTO_ONLY

    again = _HTTP([_Response(lines=openai_done)])
    await _run(client, again, model="unlisted/model", tools=[TOOL], tool_choice=choice)
    assert again.requests[0]["body"]["tool_choice"] == "auto"


async def test_unrelated_failure_does_not_probe():
    fake = _HTTP([_Response(404, error='{"error":"model not found"}')])
    with pytest.raises(LLMCallError):
        await _run(LLMClient("https://custom.test/prefix"), fake, model="missing")
    assert len(fake.requests) == 1
