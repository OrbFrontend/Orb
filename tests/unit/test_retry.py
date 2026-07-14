"""Unit tests for transient-error retry (RetryPolicy + the LLMClient retry loop).

RetryPolicy decides whether an error is retryable and how long to wait; the loop
in LLMClient.complete / complete_raw re-issues a failed request only while no
event has been streamed yet. Tests patch the documented transport seams
(``_complete_chat`` / ``_stream_completion``) so no sockets are touched, and use
``delay=0`` so retries are instant.
"""

from __future__ import annotations

import httpx
import pytest

from backend.inference.client import AbortToken, LLMClient
from backend.inference.retry import RETRYABLE_STATUS, RetryPolicy


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


async def _drain(agen):
    return [e async for e in agen]


def _chat_script(client: LLMClient, script: list) -> dict:
    """Fake ``_complete_chat``: on call i, raise ``script[i]`` if it is an exception,
    else yield the events in ``script[i]``. The last entry repeats. Returns a call
    counter so a test can assert how many attempts happened."""
    calls = {"n": 0}

    async def fake_chat(*_a, **_k):
        i = calls["n"]
        calls["n"] += 1
        action = script[i] if i < len(script) else script[-1]
        if isinstance(action, BaseException):
            raise action
        for ev in action:
            yield ev

    client._complete_chat = fake_chat  # type: ignore[method-assign]
    return calls


# -- RetryPolicy.should_retry -------------------------------------------------


def test_should_retry_disabled_never_retries():
    policy = RetryPolicy(enabled=False)
    assert policy.should_retry(_status_error(503)) is False
    assert policy.should_retry(httpx.ConnectError("x")) is False


def test_should_retry_status_codes():
    policy = RetryPolicy(enabled=True)
    for code in (408, 429, 500, 502, 503, 504, 529):
        assert policy.should_retry(_status_error(code)) is True, code
    for code in (400, 401, 403, 404, 409, 422):
        assert policy.should_retry(_status_error(code)) is False, code


def test_should_retry_transport_errors():
    policy = RetryPolicy(enabled=True)
    assert policy.should_retry(httpx.ConnectError("x")) is True
    assert policy.should_retry(httpx.ConnectTimeout("x")) is True
    assert policy.should_retry(httpx.ReadError("x")) is True
    assert policy.should_retry(httpx.ReadTimeout("x")) is True
    assert policy.should_retry(httpx.RemoteProtocolError("x")) is True
    assert policy.should_retry(httpx.PoolTimeout("x")) is True
    # A transport error deliberately excluded (client-side write) is not retried.
    assert policy.should_retry(httpx.WriteError("x")) is False
    # A completely unrelated exception is not retried.
    assert policy.should_retry(ValueError("x")) is False


# -- RetryPolicy.from_settings ------------------------------------------------


def test_from_settings_reads_values():
    policy = RetryPolicy.from_settings({"retry_enabled": 1, "retry_count": 3, "retry_delay_seconds": 2.5})
    assert policy.enabled is True
    assert policy.count == 3
    assert policy.delay == 2.5
    assert policy.status_codes == RETRYABLE_STATUS


def test_from_settings_defaults_when_keys_absent():
    policy = RetryPolicy.from_settings({})
    assert policy.enabled is False
    assert policy.count == 10
    assert policy.delay == 5.0


def test_from_settings_degrades_on_bad_values():
    # None / negative must clamp to a safe shape rather than raise on the hot path.
    policy = RetryPolicy.from_settings({"retry_enabled": 0, "retry_count": None, "retry_delay_seconds": -4})
    assert policy.count == 0
    assert policy.delay == 0.0


# -- LLMClient retry loop -----------------------------------------------------


async def test_retries_then_succeeds():
    client = LLMClient("http://x/v1", retry=RetryPolicy(enabled=True, count=5, delay=0))
    done = {"type": "done", "message": {"content": "ok"}, "usage": None}
    calls = _chat_script(client, [_status_error(503), _status_error(503), [{"type": "content", "delta": "ok"}, done]])
    events = await _drain(client.complete([], "m"))
    assert calls["n"] == 3
    assert events[-1] == done


async def test_disabled_policy_raises_on_first_error():
    client = LLMClient("http://x/v1")  # default policy: disabled
    calls = _chat_script(client, [_status_error(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "m"))
    assert calls["n"] == 1


async def test_no_retry_after_event_streamed():
    client = LLMClient("http://x/v1", retry=RetryPolicy(enabled=True, count=5, delay=0))

    async def fake_chat(*_a, **_k):
        # A delta reaches the caller, THEN the stream fails: retrying would double it.
        yield {"type": "content", "delta": "partial"}
        raise _status_error(503)

    client._complete_chat = fake_chat  # type: ignore[method-assign]
    seen = []
    with pytest.raises(httpx.HTTPStatusError):
        async for event in client.complete([], "m"):
            seen.append(event)
    assert seen == [{"type": "content", "delta": "partial"}]


async def test_exhausts_retries_then_raises():
    client = LLMClient("http://x/v1", retry=RetryPolicy(enabled=True, count=2, delay=0))
    calls = _chat_script(client, [_status_error(503)])  # always fails
    with pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "m"))
    assert calls["n"] == 3  # initial attempt + 2 retries


async def test_non_retryable_status_not_retried():
    client = LLMClient("http://x/v1", retry=RetryPolicy(enabled=True, count=5, delay=0))
    calls = _chat_script(client, [_status_error(400)])
    with pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "m"))
    assert calls["n"] == 1


async def test_transport_error_is_retried():
    client = LLMClient("http://x/v1", retry=RetryPolicy(enabled=True, count=3, delay=0))
    done = {"type": "done", "message": {"content": "ok"}, "usage": None}
    calls = _chat_script(client, [httpx.ConnectError("refused"), [done]])
    events = await _drain(client.complete([], "m"))
    assert calls["n"] == 2
    assert events[-1] == done


async def test_aborted_client_does_not_retry():
    token = AbortToken()
    token.abort()
    client = LLMClient("http://x/v1", abort_token=token, retry=RetryPolicy(enabled=True, count=5, delay=0))
    calls = _chat_script(client, [_status_error(503)])
    with pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "m"))
    assert calls["n"] == 1  # is_aborted short-circuits the retry decision


async def test_abort_during_delay_stops_retry():
    client = LLMClient("http://x/v1", retry=RetryPolicy(enabled=True, count=5, delay=1))
    calls = _chat_script(client, [_status_error(503)])  # always fails

    async def aborted_wait(_delay):
        return False  # simulate the abort signal firing during the wait

    client._sleep_or_abort = aborted_wait  # type: ignore[method-assign]
    with pytest.raises(httpx.HTTPStatusError):
        await _drain(client.complete([], "m"))
    assert calls["n"] == 1  # wait aborted -> no further attempts


async def test_complete_raw_is_retried():
    client = LLMClient("http://x/v1", completion_mode="text", retry=RetryPolicy(enabled=True, count=3, delay=0))
    calls = {"n": 0}

    async def fake_stream(_url, _body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _status_error(503)
        yield {"content": "hi", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]
    events = await _drain(client.complete_raw("p", "m"))
    assert calls["n"] == 2
    assert events[-1]["type"] == "done"
    assert events[-1]["message"]["content"] == "hi"


# -- _sleep_or_abort ----------------------------------------------------------


async def test_sleep_or_abort_true_when_not_aborted():
    client = LLMClient("http://x/v1")
    assert await client._sleep_or_abort(0) is True


async def test_sleep_or_abort_false_when_already_aborted():
    token = AbortToken()
    token.abort()
    client = LLMClient("http://x/v1", abort_token=token)
    # A long delay still returns at once (False) because the event is already set.
    assert await client._sleep_or_abort(30) is False
