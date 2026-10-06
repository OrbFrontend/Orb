"""Stop interrupts silent HTTP waits and closes the model request before settlement."""

import asyncio
import contextlib
import json

import httpx
import pytest

from backend.inference.client import LLMClient


def _mock_http(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs))


@pytest.mark.parametrize(
    ("mode", "blocked_path"),
    [("chat", "/v1/chat/completions"), ("raw", "/completion"), ("text", "/props"), ("text", "/apply-template")],
)
async def test_stop_interrupts_setup_without_waiting_for_the_server(monkeypatch, mode, blocked_path):
    reached, cancelled = asyncio.Event(), asyncio.Event()
    paths: list[str] = []

    async def handle(request):
        paths.append(request.url.path)
        if request.url.path == blocked_path:
            reached.set()
            try:
                await asyncio.Event().wait()  # The server never sends headers.
            finally:
                cancelled.set()
        return httpx.Response(200, json={"chat_template": "template"})

    _mock_http(monkeypatch, handle)
    client = LLMClient("http://model/v1", completion_mode="text" if mode == "text" else "chat")
    stream = client.complete_raw("prompt", "m") if mode == "raw" else client.complete([], "m")

    async def consume():
        return [event async for event in stream]

    reader = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(reached.wait(), 2)
        client.abort()
        assert await asyncio.wait_for(reader, 1) == []
        assert cancelled.is_set(), "Stop returned before the HTTP request was cleaned up"
        assert paths[-1] == blocked_path, "a stopped call started another request"
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


@pytest.mark.parametrize("mode", ["chat", "raw", "tool", "error_body"])
async def test_stop_closes_a_silent_response_and_keeps_only_delivered_deltas(monkeypatch, mode):
    waiting, cancelled, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    paths: list[str] = []
    seen: list[dict] = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            if mode != "error_body":
                if mode == "tool":
                    chunk = {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "direct_scene"}}]}}]}
                elif mode == "chat":
                    chunk = {"choices": [{"delta": {"content": "partial prose"}}]}
                else:
                    chunk = {"content": "partial prose"}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            waiting.set()
            try:
                await asyncio.Event().wait()  # No next token (or error body).
            finally:
                cancelled.set()

        async def aclose(self):
            closed.set()

    async def handle(request):
        paths.append(request.url.path)
        return httpx.Response(503 if mode == "error_body" else 200, stream=Body())

    _mock_http(monkeypatch, handle)
    client = LLMClient("http://model/v1")
    stream = client.complete_raw("prompt", "m") if mode == "raw" else client.complete([], "m")

    async def consume():
        async for event in stream:
            seen.append(event)

    reader = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        client.abort()
        await asyncio.wait_for(reader, 1)
        assert seen == ([] if mode in {"error_body", "tool"} else [{"type": "content", "delta": "partial prose"}])
        assert cancelled.is_set()
        assert closed.is_set(), "Stop left the upstream response open"
        assert len(paths) == 1, "Stop retried an interrupted request"
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader
