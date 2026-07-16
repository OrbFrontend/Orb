"""Per-model reasoning-effort: reasoning_cfg shape, body injection, factory threading."""

from __future__ import annotations

from unittest.mock import patch

import backend.inference.client as llm_mod
from backend.inference.client import (
    LLMClient,
    agent_client_from_settings,
    apply_reasoning_effort,
    client_from_settings,
    reasoning_cfg,
)


def _enabled_body() -> dict:
    return {
        "model": "m",
        "messages": [],
        "reasoning": {"enabled": True},
        "thinking": {"type": "enabled"},
    }


def test_reasoning_cfg_on_carries_no_effort():
    cfg = reasoning_cfg(True)
    assert cfg["reasoning"] == {"enabled": True}
    assert "effort" not in cfg["reasoning"]


def test_reasoning_cfg_off_unchanged():
    cfg = reasoning_cfg(False)
    assert cfg["reasoning"] == {"effort": "none", "enabled": False}
    assert cfg["thinking"] == {"type": "disabled"}


def test_apply_level_sets_both_dialects():
    body = _enabled_body()
    shared = body["reasoning"]
    apply_reasoning_effort(body, "high")
    assert body["reasoning_effort"] == "high"
    assert body["reasoning"] == {"enabled": True, "effort": "high"}
    # The caller's reasoning dict is shared across calls; it must not be mutated.
    assert shared == {"enabled": True}


def test_apply_skips_reasoning_off_calls():
    body = {"model": "m", "reasoning": {"effort": "none", "enabled": False}}
    apply_reasoning_effort(body, "high")
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"effort": "none", "enabled": False}


def test_apply_skips_bodies_without_reasoning():
    body = {"model": "m", "messages": []}
    apply_reasoning_effort(body, "high")
    assert body == {"model": "m", "messages": []}


def test_apply_empty_effort_is_noop():
    body = _enabled_body()
    apply_reasoning_effort(body, "")
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"enabled": True}


def test_apply_custom_sends_exact_param_json_decoded():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "thinking_budget", "4096")
    assert body["thinking_budget"] == 4096
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"enabled": True}


def test_apply_custom_object_value():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "reasoning", '{"effort": "xhigh"}')
    assert body["reasoning"] == {"effort": "xhigh"}


def test_apply_custom_bare_word_stays_string():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "reasoning_effort", "max")
    assert body["reasoning_effort"] == "max"


def test_apply_custom_without_param_is_noop():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "", "4096")
    assert body == _enabled_body()


def test_client_factory_threads_effort():
    settings = {
        "endpoint_url": "http://localhost:5000/v1",
        "reasoning_effort": "xhigh",
        "reasoning_effort_param": "p",
        "reasoning_effort_value": "v",
    }
    client = client_from_settings(settings)
    assert client.reasoning_effort == "xhigh"
    assert client.reasoning_effort_param == "p"
    assert client.reasoning_effort_value == "v"


def test_agent_factory_falls_back_to_writer_effort():
    settings = {"endpoint_url": "http://localhost:5000/v1", "reasoning_effort": "low"}
    client = agent_client_from_settings(settings)
    assert client.reasoning_effort == "low"


def test_agent_factory_prefers_agent_effort():
    settings = {
        "endpoint_url": "http://localhost:5000/v1",
        "reasoning_effort": "low",
        "agent_reasoning_effort": "high",
    }
    client = agent_client_from_settings(settings)
    assert client.reasoning_effort == "high"


# ── Wire-level: the client attribute must land in the outbound body ──────────


class _FakeStream:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}'
        yield "data: [DONE]"


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        self.bodies = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.bodies.append(dict(json or {}))
        return _FakeStream()


async def _wire_body(client: LLMClient, **params) -> dict:
    fake = _FakeAsyncClient()
    with patch.object(llm_mod.httpx, "AsyncClient", lambda *a, **k: fake):
        async for _ in client.complete([], "m", **params):
            pass
    assert len(fake.bodies) == 1
    return fake.bodies[0]


async def test_wire_level_reaches_body():
    client = LLMClient("http://localhost:5000/v1", reasoning_effort="high")
    body = await _wire_body(client, **reasoning_cfg(True))
    assert body["reasoning_effort"] == "high"
    assert body["reasoning"] == {"enabled": True, "effort": "high"}


async def test_wire_custom_param_reaches_body():
    client = LLMClient(
        "http://localhost:5000/v1",
        reasoning_effort="custom",
        reasoning_effort_param="thinking_budget",
        reasoning_effort_value="2048",
    )
    body = await _wire_body(client, **reasoning_cfg(True))
    assert body["thinking_budget"] == 2048
    assert "reasoning_effort" not in body


async def test_wire_reasoning_off_sends_no_effort():
    client = LLMClient("http://localhost:5000/v1", reasoning_effort="high")
    body = await _wire_body(client, **reasoning_cfg(False))
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"effort": "none", "enabled": False}


async def test_wire_deepseek_profile_strips_effort():
    # DeepSeek's allowlist passes 'thinking' but drops the OpenAI/OpenRouter
    # effort dialects; the injection must not survive the profile.
    client = LLMClient("https://api.deepseek.com/v1", reasoning_effort="high")
    body = await _wire_body(client, **reasoning_cfg(True))
    assert "reasoning_effort" not in body
    assert "reasoning" not in body
    assert body["thinking"] == {"type": "enabled"}
