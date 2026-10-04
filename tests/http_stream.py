"""Scripted HTTP streams and request capture for inference contract tests."""

from unittest.mock import patch

import httpx

from backend.inference import client as llm_module

DONE_LINES = ['data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}', "data: [DONE]"]


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class StreamResponse(AsyncContext):
    def __init__(self, status=200, error="", lines=()):
        self.status_code = status
        self.error = error
        self.lines = list(lines)

    async def aread(self):
        return self.error.encode()

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=self)


class ScriptedClient(AsyncContext):
    def __init__(self, responses, *, repeat_last=False):
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.bodies = []
        self.requests = []

    def stream(self, method, url, json=None, headers=None):
        body = dict(json or {})
        self.bodies.append(body)
        self.requests.append({"method": method, "url": url, "body": body, "headers": dict(headers or {})})
        response = self.responses[0]
        if len(self.responses) > 1 or not self.repeat_last:
            self.responses.pop(0)
        return response


async def capture_wire_body(client, **params):
    fake = ScriptedClient([StreamResponse(lines=DONE_LINES)], repeat_last=True)
    with patch.object(llm_module.httpx, "AsyncClient", lambda *args, **kwargs: fake):
        async for _ in client.complete([], "m", **params):
            pass
    assert len(fake.bodies) == 1
    return fake.bodies[0]
