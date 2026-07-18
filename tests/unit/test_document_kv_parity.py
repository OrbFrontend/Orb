"""Doc-mode KV-cache parity: the Output Auditor's patch call must byte-extend
the generation call (docs/architecture/kv-cache.md — extend, don't fork).

Drives the REAL DocumentContinuer.stream for generation and the REAL
patch_document for the fix call on one capturing stub, then asserts per shape
that the patch's wire inputs are a strict extension of the generation's:

* text+raw       — patch raw prompt startswith(generation prompt + draft core)
* text+assisted  — render_prompt re-runs the EXACT generation render inputs
                   (same messages, same prefill) and the raw patch prompt
                   byte-extends that render
* chat shapes    — the patch message list replays the generation messages
                   verbatim (serialized comparison), with tools kept out of the
                   prompt (tools_in_prompt=False) and identical reasoning kwargs

The sibling of test_kv_cache_invariants.py for the documents slice.
"""

from __future__ import annotations

import json

from backend.features.documents.audit import patch_document
from backend.features.documents.continuation import DocumentContinuer
from backend.inference.kv_tracker import _serialize_messages

_BANNED = "shivers down her spine"
_BANK = [[_BANNED]]
_SETTINGS = {"temperature": 0.5, "max_tokens": 256}

# Complete final sentence → truncated=False keeps core == draft.
_DRAFT = f"She felt {_BANNED} in the dark. He kept walking."
# Mid-sentence tail for the truncated case; core must stay a prefix.
_DRAFT_CUT = f"She felt {_BANNED} in the dark. He kept wal"

_RAW_PROMPT = "The road stretched on toward the mountains. "
_ASSISTED_PROMPT = "### USER: continue the story\nThe road stretched on"


class _WireCapture:
    """Records every wire call; first complete/complete_raw is the generation,
    the next one is the patch. Streams _DRAFT (or *draft*) as the generation
    output and answers the patch with an empty patches object."""

    def __init__(self, completion_mode: str, draft: str = _DRAFT):
        self.completion_mode = completion_mode
        self.draft = draft
        self.message_calls: list[dict] = []
        self.raw_calls: list[dict] = []
        self.render_calls: list[dict] = []

    def _is_generation(self) -> bool:
        return not self.message_calls and not self.raw_calls

    async def complete(self, messages, model, tools=None, tool_choice=None, **params):
        generation = self._is_generation()
        self.message_calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "tool_choice": tool_choice, "params": dict(params)}
        )
        if generation:
            yield {"type": "content", "delta": self.draft}
            yield {"type": "done", "message": {"content": self.draft, "finish_reason": "stop"}}
        else:
            yield {
                "type": "done",
                "message": {
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {"name": "editor_apply_patch", "arguments": json.dumps({"patches": []})},
                        }
                    ]
                },
            }

    async def complete_raw(self, prompt, model, **params):
        generation = self._is_generation()
        self.raw_calls.append({"prompt": prompt, "params": dict(params)})
        if generation:
            yield {"type": "content", "delta": self.draft}
            yield {"type": "done", "message": {"content": self.draft, "finish_reason": "stop"}}
        else:
            yield {"type": "done", "message": {"content": json.dumps({"patches": []})}}

    async def render_prompt(self, messages, *, prefill=None, reasoning=False):
        self.render_calls.append({"messages": [dict(m) for m in messages], "prefill": prefill, "reasoning": reasoning})
        return f"<render|{_serialize_messages(messages)}|{prefill or ''}>"


async def _generate(client: _WireCapture, prompt: str, assisted: bool) -> str:
    out = ""
    async for chunk in DocumentContinuer(client, _SETTINGS).stream(prompt, "m", assisted=assisted):
        if chunk["type"] == "content":
            out += chunk["delta"]
    return out


async def _roundtrip(client: _WireCapture, prompt: str, *, assisted: bool, truncated: bool = False) -> str:
    draft = await _generate(client, prompt, assisted)
    await patch_document(client, "m", draft, prompt, _BANK, None, _SETTINGS, assisted=assisted, truncated=truncated)
    return draft


async def test_text_raw_patch_prompt_extends_generation_prompt():
    client = _WireCapture("text")
    draft = await _roundtrip(client, _RAW_PROMPT, assisted=False)

    gen, patch = client.raw_calls
    assert gen["prompt"] == _RAW_PROMPT
    # Byte-extension: generation prompt + the model's own draft, no joiner.
    assert patch["prompt"].startswith(_RAW_PROMPT + draft)
    assert client.message_calls == []


async def test_text_raw_truncated_core_stays_a_prefix():
    client = _WireCapture("text", draft=_DRAFT_CUT)
    draft = await _roundtrip(client, _RAW_PROMPT, assisted=False, truncated=True)

    patch = client.raw_calls[1]
    core = f"She felt {_BANNED} in the dark. "  # trimmed at the sentence boundary
    assert draft == _DRAFT_CUT
    assert patch["prompt"].startswith(_RAW_PROMPT + core)
    # The dangling fragment never enters the patch prompt (it reattaches after).
    assert "He kept wal" not in patch["prompt"]


async def test_text_assisted_patch_rerenders_exact_generation_inputs():
    client = _WireCapture("text")
    draft = await _roundtrip(client, _ASSISTED_PROMPT, assisted=True)

    gen = client.message_calls[0]
    # The patch re-render uses the IDENTICAL messages + prefill the generation
    # call sent — template quirks then reproduce byte-for-byte.
    assert client.render_calls == [{"messages": gen["messages"], "prefill": gen["params"]["prefill"], "reasoning": False}]
    rendered = f"<render|{_serialize_messages(gen['messages'])}|{gen['params']['prefill']}>"
    assert client.raw_calls[0]["prompt"].startswith(rendered + draft)


async def test_chat_shapes_patch_messages_replay_generation():
    for prompt, assisted in ((_RAW_PROMPT, False), (_ASSISTED_PROMPT, True)):
        client = _WireCapture("chat")
        draft = await _roundtrip(client, prompt, assisted=assisted)

        gen, patch = client.message_calls
        # Serialized prefix: the patch conversation replays the generation
        # conversation verbatim, then appends draft + fix instruction.
        assert _serialize_messages(patch["messages"]).startswith(_serialize_messages(gen["messages"])), (prompt, assisted)
        assert patch["messages"][len(gen["messages"])] == {"role": "assistant", "content": draft}
        # Generation sent no tools, so the patch schema must stay out of the
        # rendered prompt; forcing rides response_format via this flag.
        assert gen["tools"] is None
        assert patch["params"]["tools_in_prompt"] is False
        # Same reasoning kwargs on both calls — a mismatch would fork the
        # template render (enable_thinking bytes).
        assert patch["params"]["chat_template_kwargs"] == gen["params"]["chat_template_kwargs"], (prompt, assisted)
