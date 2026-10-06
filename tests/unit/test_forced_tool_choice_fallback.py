"""Tests for the forced-tool_choice fallback (endpoint_profiles + llm_client).

Covers:
  - OpenRouter catalog models are not encoded as permanent profiles.
  - LLMClient.complete()'s provider-gated, error-specific retry: drops
    tool_choice once for the matching OpenRouter 404 (regardless of its value),
    raises immediately for unrelated 404s, and never retries when no
    tool_choice was sent.
"""

from unittest.mock import patch

import httpx
import pytest

from backend.inference import LLMClient
from backend.inference import client as llm_mod
from backend.inference import endpoint_profiles as ep_mod
from backend.inference.endpoint_profiles import _is_tool_choice_unsupported, is_forced_tool_choice, profile_for
from tests.http_stream import ScriptedClient as _FakeAsyncClient
from tests.http_stream import StreamResponse as _FakeStreamResponse

# ---- Layer 1: ModelProfile / PROFILES -------------------------------------


def test_honors_forced_tool_choice_dry_run():
    from backend.inference.endpoint_profiles import honors_forced_tool_choice

    thinking_on = {"thinking": {"type": "enabled"}}
    # DeepSeek 400s on a forced tool_choice whenever thinking is enabled, so the
    # profile coerces it; without thinking the forcing survives.
    assert not honors_forced_tool_choice("https://api.deepseek.com", "deepseek-v4-pro", thinking_on)
    assert honors_forced_tool_choice("https://api.deepseek.com", "deepseek-v4-pro", {"thinking": {"type": "disabled"}})
    assert honors_forced_tool_choice("https://api.deepseek.com", "deepseek-v4-pro")
    # deepseek-reasoner drops it unconditionally; unknown endpoints pass through.
    assert not honors_forced_tool_choice("https://api.deepseek.com", "deepseek-reasoner")
    assert honors_forced_tool_choice("http://localhost:5000/v1", "any-model", thinking_on)


def test_openrouter_unlisted_model_is_passthrough():
    assert profile_for("https://openrouter.ai/api/v1", "some/other-model") is None


# ---- helpers ---------------------------------------------------------------


def test_is_tool_choice_unsupported_signature():
    txt = "No endpoints found that support the provided 'tool_choice' value."
    assert _is_tool_choice_unsupported(404, txt)
    assert _is_tool_choice_unsupported(400, txt)
    assert not _is_tool_choice_unsupported(404, "model not found")


def test_is_forced_tool_choice():
    assert is_forced_tool_choice({"type": "function", "function": {"name": "x"}})
    assert is_forced_tool_choice("required")
    assert not is_forced_tool_choice("auto")
    assert not is_forced_tool_choice(None)


# ---- Layer 2: LLMClient retry ---------------------------------------------


_DONE_LINES = ['data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}', "data: [DONE]"]

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
    ep_mod._TOOL_CHOICE_UNSUPPORTED.clear()
    yield
    ep_mod._TOOL_CHOICE_UNSUPPORTED.clear()


@pytest.mark.parametrize("tc", [_FORCED_TC, "required", "none", "auto"])
async def test_openrouter_404_retries_by_dropping_tool_choice(tc):
    # The 404 is value-agnostic: any tool_choice the routed provider can't honor
    # (forced dict, "required", or even "none") recovers by dropping the param.
    fake, p = _client_factory([_FakeStreamResponse(404, error=_OR_404), _FakeStreamResponse(200, lines=_DONE_LINES)])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p:
        events = await _drain(client.complete([], "any/model", tool_choice=tc))
    # Two attempts: first sends the value, second omits tool_choice entirely.
    assert len(fake.bodies) == 2
    assert fake.bodies[0]["tool_choice"] == tc
    assert "tool_choice" not in fake.bodies[1]
    assert events[-1]["type"] == "done"
    # Pair remembered for the session.
    assert ("https://openrouter.ai/api/v1", "any/model") in ep_mod._TOOL_CHOICE_UNSUPPORTED


async def test_session_cache_drops_up_front():
    ep_mod._TOOL_CHOICE_UNSUPPORTED.add(("https://openrouter.ai/api/v1", "any/model"))
    fake, p = _client_factory([_FakeStreamResponse(200, lines=_DONE_LINES)])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p:
        await _drain(client.complete([], "any/model", tool_choice=_FORCED_TC))
    # Single request, tool_choice dropped before sending.
    assert len(fake.bodies) == 1
    assert "tool_choice" not in fake.bodies[0]


async def test_unrelated_404_raises_immediately():
    fake, p = _client_factory([_FakeStreamResponse(404, error="model not found")])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p, pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "bad/model", tool_choice=_FORCED_TC))
    assert len(fake.bodies) == 1  # no retry


async def test_non_openrouter_404_not_retried():
    fake, p = _client_factory([_FakeStreamResponse(404, error=_OR_404)])
    client = LLMClient("http://localhost:8080/v1")
    with p, pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "llama", tool_choice=_FORCED_TC))
    assert len(fake.bodies) == 1


async def test_no_retry_when_no_tool_choice_sent():
    fake, p = _client_factory([_FakeStreamResponse(404, error=_OR_404)])
    client = LLMClient("https://openrouter.ai/api/v1")
    with p, pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "any/model"))
    assert len(fake.bodies) == 1
    assert "tool_choice" not in fake.bodies[0]
