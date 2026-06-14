"""Tests for the forced-tool_choice fallback (endpoint_profiles + llm_client).

Covers:
  - ModelProfile.allow_extra=None disables drop-filtering entirely.
  - The OpenRouter PROFILES entry coerces forced tool_choice proactively.
  - LLMClient.complete()'s provider-gated, error-specific retry: drops
    tool_choice once for the matching OpenRouter 404 (regardless of its value),
    raises immediately for unrelated 404s, and never retries when no
    tool_choice was sent.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from backend import llm_client as llm_mod
from backend.endpoint_profiles import ModelProfile, is_forced_tool_choice, profile_for
from backend.llm_client import (
    LLMClient,
    _is_tool_choice_unsupported,
)


# ---- Layer 1: ModelProfile / PROFILES -------------------------------------


def test_allow_extra_none_drops_nothing():
    prof = ModelProfile(allow_extra=None, allow_forced_tool_choice=True)
    body = {"model": "m", "messages": [], "temperature": 0.7, "reasoning": {}, "weird": 1}
    actions = prof.apply(body)
    assert "temperature" in body and "reasoning" in body and "weird" in body
    assert not any("dropped" in a for a in actions)


def test_allow_extra_frozenset_still_drops():
    prof = ModelProfile(allow_extra=frozenset({"temperature"}))
    body = {"model": "m", "messages": [], "temperature": 0.7, "weird": 1}
    prof.apply(body)
    assert "temperature" in body
    assert "weird" not in body


def test_openrouter_minimax_profile_coerces_forced_tool_choice():
    prof = profile_for("https://openrouter.ai/api/v1", "minimax/minimax-m3")
    assert prof is not None
    body = {
        "model": "minimax/minimax-m3",
        "messages": [],
        "tool_choice": {"type": "function", "function": {"name": "direct_scene"}},
        "temperature": 0.7,
    }
    prof.apply(body)
    assert body["tool_choice"] == "auto"
    assert body["temperature"] == 0.7  # nothing dropped


def test_openrouter_unlisted_model_is_passthrough():
    assert profile_for("https://openrouter.ai/api/v1", "some/other-model") is None


# ---- helpers ---------------------------------------------------------------


def test_is_tool_choice_unsupported_signature():
    txt = "No endpoints found that support the provided 'tool_choice' value."
    assert _is_tool_choice_unsupported(404, txt)
    assert not _is_tool_choice_unsupported(400, txt)
    assert not _is_tool_choice_unsupported(404, "model not found")


def test_is_forced_tool_choice():
    assert is_forced_tool_choice({"type": "function", "function": {"name": "x"}})
    assert is_forced_tool_choice("required")
    assert not is_forced_tool_choice("auto")
    assert not is_forced_tool_choice(None)


# ---- Layer 2: LLMClient retry ---------------------------------------------


class _FakeStreamResponse:
    """Async-context-manager mimicking httpx's streaming response."""

    def __init__(self, status_code, err_text="", lines=()):
        self.status_code = status_code
        self._err_text = err_text
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._err_text.encode("utf-8")

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=None)


class _FakeAsyncClient:
    """Replaces httpx.AsyncClient; serves a queued response per stream() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.bodies = []  # captured request bodies per attempt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, json=None, headers=None):
        self.bodies.append(dict(json))
        return self._responses.pop(0)


_DONE_LINES = [
    'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}',
    "data: [DONE]",
]

_OR_404 = "No endpoints found that support the provided 'tool_choice' value."

_FORCED_TC = {"type": "function", "function": {"name": "direct_scene"}}


async def _drain(gen):
    return [e async for e in gen]


def _client_factory(responses):
    """Patch httpx.AsyncClient to return a shared fake; return that fake."""
    fake = _FakeAsyncClient(responses)
    return fake, patch.object(llm_mod.httpx, "AsyncClient", lambda *a, **k: fake)


@pytest.fixture(autouse=True)
def _clear_session_cache():
    llm_mod._TOOL_CHOICE_UNSUPPORTED.clear()
    yield
    llm_mod._TOOL_CHOICE_UNSUPPORTED.clear()


@pytest.mark.parametrize("tc", [_FORCED_TC, "required", "none", "auto"])
async def test_openrouter_404_retries_by_dropping_tool_choice(tc):
    # The 404 is value-agnostic: any tool_choice the routed provider can't honor
    # (forced dict, "required", or even "none") recovers by dropping the param.
    fake, p = _client_factory(
        [
            _FakeStreamResponse(404, err_text=_OR_404),
            _FakeStreamResponse(200, lines=_DONE_LINES),
        ]
    )
    client = LLMClient("https://openrouter.ai/api/v1")
    with p:
        events = await _drain(client.complete([], "any/model", tool_choice=tc))
    # Two attempts: first sends the value, second omits tool_choice entirely.
    assert len(fake.bodies) == 2
    assert fake.bodies[0]["tool_choice"] == tc
    assert "tool_choice" not in fake.bodies[1]
    assert events[-1]["type"] == "done"
    # Pair remembered for the session.
    assert ("https://openrouter.ai/api/v1", "any/model") in llm_mod._TOOL_CHOICE_UNSUPPORTED


async def test_session_cache_drops_up_front():
    llm_mod._TOOL_CHOICE_UNSUPPORTED.add(("https://openrouter.ai/api/v1", "any/model"))
    fake, p = _client_factory([_FakeStreamResponse(200, lines=_DONE_LINES)])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p:
        await _drain(client.complete([], "any/model", tool_choice=_FORCED_TC))
    # Single request, tool_choice dropped before sending.
    assert len(fake.bodies) == 1
    assert "tool_choice" not in fake.bodies[0]


async def test_unrelated_404_raises_immediately():
    fake, p = _client_factory([_FakeStreamResponse(404, err_text="model not found")])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p, pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "bad/model", tool_choice=_FORCED_TC))
    assert len(fake.bodies) == 1  # no retry


async def test_non_openrouter_404_not_retried():
    fake, p = _client_factory([_FakeStreamResponse(404, err_text=_OR_404)])
    client = LLMClient("http://localhost:8080/v1")
    with p, pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "llama", tool_choice=_FORCED_TC))
    assert len(fake.bodies) == 1


async def test_no_retry_when_no_tool_choice_sent():
    fake, p = _client_factory([_FakeStreamResponse(404, err_text=_OR_404)])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p, pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "any/model"))
    assert len(fake.bodies) == 1
    assert "tool_choice" not in fake.bodies[0]
