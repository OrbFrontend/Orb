"""Pass-shaped calls through real LLMClient protocol adapters.

The normal integration mock replaces ``LLMClient.complete`` wholesale. This
test stubs httpx one seam lower so Director/Writer/Editor request shapes and
stream translation run for OpenAI, Anthropic, and Gemini transports.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.inference import client as llm_mod
from backend.inference import endpoint_profiles as ep
from backend.inference.client import LLMClient, parse_tool_calls


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        },
    }


TOOLS = [_tool("direct_scene"), _tool("editor_apply_patch")]
DIRECTOR = {"type": "function", "function": {"name": "direct_scene"}}
EDITOR = {"type": "function", "function": {"name": "editor_apply_patch"}}


class _Response:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class _Transport:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.requests.append({"url": url, "body": dict(json or {}), "headers": dict(headers or {})})
        return _Response(self.scripts.pop(0))


def _data(payload: dict) -> str:
    return f"data: {json.dumps(payload)}"


def _openai_tool(name: str) -> list[str]:
    call = {
        "index": 0,
        "id": f"call-{name}",
        "function": {"name": name, "arguments": '{"value":"ok"}'},
    }
    return [_data({"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}), "data: [DONE]"]


def _openai_text() -> list[str]:
    return [_data({"choices": [{"delta": {"content": "draft"}, "finish_reason": "stop"}]}), "data: [DONE]"]


def _anthropic_tool(name: str) -> list[str]:
    return [
        _data(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"call-{name}", "name": name, "input": {}},
            }
        ),
        _data(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"value":"ok"}'},
            }
        ),
        _data({"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
        _data({"type": "message_stop"}),
    ]


def _anthropic_text() -> list[str]:
    return [
        _data({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "draft"}}),
        _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        _data({"type": "message_stop"}),
    ]


async def _complete(client: LLMClient, model: str, choice: dict | str) -> list[dict]:
    return [
        event
        async for event in client.complete(
            [{"role": "system", "content": "system"}, {"role": "user", "content": "turn"}],
            model,
            tools=TOOLS,
            tool_choice=choice,
            max_tokens=100,
        )
    ]


@pytest.mark.parametrize(
    ("provider", "endpoint", "model"),
    [
        ("openai", "https://openai.test/v1/chat/completions", "openai-model"),
        ("anthropic", "https://api.anthropic.com/v1/messages", "claude-haiku-4-5"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3-pro"),
    ],
)
async def test_director_writer_editor_calls_cross_protocol_boundary(provider, endpoint, model):
    ep._RESOLVED_ROUTES.clear()
    if provider == "anthropic":
        scripts = [_anthropic_tool("direct_scene"), _anthropic_text(), _anthropic_tool("editor_apply_patch")]
    elif provider == "gemini":
        # Gemini forced calls use strict response_format, whose JSON content is
        # re-synthesized into Orb's ordinary tool-call message.
        scripts = [
            [_data({"choices": [{"delta": {"content": '{"value":"ok"}'}, "finish_reason": "stop"}]}), "data: [DONE]"],
            _openai_text(),
            [_data({"choices": [{"delta": {"content": '{"value":"ok"}'}, "finish_reason": "stop"}]}), "data: [DONE]"],
        ]
    else:
        scripts = [_openai_tool("direct_scene"), _openai_text(), _openai_tool("editor_apply_patch")]

    transport = _Transport(scripts)
    client = LLMClient(endpoint, "secret")
    with patch.object(llm_mod.httpx, "AsyncClient", lambda *args, **kwargs: transport):
        director = await _complete(client, model, DIRECTOR)
        writer = await _complete(client, model, "none")
        editor = await _complete(client, model, EDITOR)

    assert parse_tool_calls(director[-1]["message"])[0]["name"] == "direct_scene"
    assert writer[-1]["message"]["content"] == "draft"
    assert parse_tool_calls(editor[-1]["message"])[0]["name"] == "editor_apply_patch"
    assert len(transport.requests) == 3

    bodies = [request["body"] for request in transport.requests]
    if provider == "anthropic":
        assert [body["tool_choice"] for body in bodies] == [
            {"type": "tool", "name": "direct_scene"},
            {"type": "none"},
            {"type": "tool", "name": "editor_apply_patch"},
        ]
        assert all(body["tools"][0]["strict"] is True for body in bodies)
    elif provider == "gemini":
        assert "response_format" in bodies[0] and "response_format" in bodies[2]
        assert "tools" not in bodies[1] and "tool_choice" not in bodies[1]
    else:
        assert [body["tool_choice"] for body in bodies] == [DIRECTOR, "none", EDITOR]
