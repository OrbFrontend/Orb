"""Unit tests for DocumentContinuer against a stub client.

Verifies the two transport branches, the exact chat-fallback message shape,
that reasoning is suppressed on the chat path, and that reasoning deltas are
dropped while content deltas pass through.
"""

from __future__ import annotations

from backend.features.documents import DOC_CHAT_INSTRUCTION, DocumentContinuer


class _StubClient:
    def __init__(self, completion_mode: str):
        self.completion_mode = completion_mode
        self.chat_calls: list[dict] = []
        self.raw_calls: list[dict] = []

    async def complete(self, messages, model, **params):
        self.chat_calls.append({"messages": messages, "model": model, "params": params})
        yield {"type": "reasoning", "delta": "thinking..."}
        yield {"type": "content", "delta": "chat-out"}
        yield {"type": "done", "message": {"content": "chat-out"}}

    async def complete_raw(self, prompt, model, **params):
        self.raw_calls.append({"prompt": prompt, "model": model, "params": params})
        yield {"type": "content", "delta": "raw-out"}
        yield {"type": "done", "message": {"content": "raw-out"}}


async def _drain(agen):
    return [x async for x in agen]


async def test_chat_path_builds_system_user_and_suppresses_thinking():
    client = _StubClient("chat")
    cont = DocumentContinuer(client, {"temperature": 0.9, "max_tokens": 100})
    out = await _drain(cont.stream("the prefix", "m"))

    assert out == ["chat-out"]  # reasoning delta dropped
    call = client.chat_calls[0]
    assert call["messages"] == [
        {"role": "system", "content": DOC_CHAT_INSTRUCTION},
        {"role": "user", "content": "the prefix"},
    ]
    # reasoning_cfg(False) spread in: thinking disabled.
    assert call["params"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}
    assert call["params"]["temperature"] == 0.9
    assert not client.raw_calls


async def test_text_path_calls_complete_raw_with_verbatim_prompt():
    client = _StubClient("text")
    cont = DocumentContinuer(client, {})
    out = await _drain(cont.stream("continue me", "m"))

    assert out == ["raw-out"]
    assert client.raw_calls[0]["prompt"] == "continue me"
    # unset max_tokens defaults to 512 (guards n_predict=-1 runaway).
    assert client.raw_calls[0]["params"]["max_tokens"] == 512
    assert not client.chat_calls
