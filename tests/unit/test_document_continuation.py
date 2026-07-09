"""Unit tests for DocumentContinuer + parse_doc_macros against a stub client.

Covers the four transport×mode branches, the exact chat-fallback and assisted
message shapes, that reasoning is suppressed on the chat/prefill paths, the
delta filter, and the full parse_doc_macros contract (alternation, defaults,
prefill extraction, macro coalescing).
"""

from __future__ import annotations

from backend.features.documents import (
    DOC_ASSIST_CONTINUE,
    DOC_ASSIST_INSTRUCTION,
    DOC_CHAT_INSTRUCTION,
    DocumentContinuer,
    parse_doc_macros,
)

_DEFAULT_USER = "Continue the text. Write several paragraphs."


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


def _assert_alternates(messages, prefill):
    """The load-bearing invariant: [system, user, assistant, user, …] — starts
    [system, user] and strictly alternates, and appending the open prefill as an
    assistant turn keeps it alternating (so the rendered template is well-formed)."""
    assert messages[0]["role"] == "system"
    roles = [m["role"] for m in messages[1:]]
    assert roles and roles[0] == "user"
    expected = ["user", "assistant"]
    for i, r in enumerate(roles):
        assert r == expected[i % 2], f"alternation broke at {i}: {roles}"
    # prefill is an *open assistant* turn appended after the messages; the last
    # message must therefore be a user turn either way.
    assert roles[-1] == "user"


# ── parse_doc_macros ─────────────────────────────────────────────────────────


def test_interleaved_notes_and_prose_alternate_in_document_order():
    text = (
        "### SYSTEM: You are a co-writer.\n"
        "### USER: Write a story about a monkey.\n"
        "Once upon a time, there lived a monkey.\n"
        "### USER: Write tersely now. Short sentences.\n"
        "The monkey woke. He"
    )
    messages, prefill = parse_doc_macros(text)
    assert messages == [
        {"role": "system", "content": "You are a co-writer."},
        {"role": "user", "content": "Write a story about a monkey."},
        {"role": "assistant", "content": "Once upon a time, there lived a monkey."},
        {"role": "user", "content": "Write tersely now. Short sentences."},
    ]
    assert prefill == "The monkey woke. He"
    _assert_alternates(messages, prefill)


def test_missing_system_defaults_to_assist_instruction():
    messages, prefill = parse_doc_macros("### USER: go\nsome prose")
    assert messages[0] == {"role": "system", "content": DOC_ASSIST_INSTRUCTION}
    assert messages[1] == {"role": "user", "content": "go"}
    assert prefill == "some prose"


def test_missing_user_inserts_default_user_turn():
    messages, prefill = parse_doc_macros("### SYSTEM: be terse\nthe body prose")
    assert messages == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": _DEFAULT_USER},
    ]
    assert prefill == "the body prose"


def test_macro_free_doc_is_backward_compat_three_turn_shape():
    # No macros at all → [system(default), default-user] + whole doc as prefill.
    doc = "Once upon a time, beneath the canopy, there lived a monkey."
    messages, prefill = parse_doc_macros(doc)
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
    ]
    assert prefill == doc


def test_leading_prose_gets_filler_user_turn():
    text = "An opening line.\n### USER: keep it dark\nmid continuation"
    messages, prefill = parse_doc_macros(text)
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
        {"role": "assistant", "content": "An opening line."},
        {"role": "user", "content": "keep it dark"},
    ]
    assert prefill == "mid continuation"
    _assert_alternates(messages, prefill)


def test_trailing_note_yields_none_prefill():
    text = "The prior paragraph.\n### USER: now write the ending"
    messages, prefill = parse_doc_macros(text)
    assert prefill is None
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
        {"role": "assistant", "content": "The prior paragraph."},
        {"role": "user", "content": "now write the ending"},
    ]
    # No prefill → messages end on a user turn (generation-prompt / fresh turn).
    assert messages[-1]["role"] == "user"


def test_consecutive_user_lines_join_into_one_turn():
    text = "### USER: first line of the note\n### USER: second line of the note\nprose tail"
    messages, prefill = parse_doc_macros(text)
    assert messages[1] == {"role": "user", "content": "first line of the note\nsecond line of the note"}
    assert prefill == "prose tail"


def test_whitespace_only_prose_between_notes_dropped_and_notes_merge():
    text = "### USER: alpha\n   \n### USER: beta\nThe tale begins"
    messages, prefill = parse_doc_macros(text)
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": "alpha\nbeta"},
    ]
    assert prefill == "The tale begins"


def test_assistant_macro_content_joins_surrounding_prose():
    text = "Chapter one.\n### ASSISTANT: The hero rose.\nAnd walked on."
    messages, prefill = parse_doc_macros(text)
    # ### ASSISTANT: is stripped and its content folded into the one prose block.
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
    ]
    assert prefill == "Chapter one.\nThe hero rose.\nAnd walked on."


def test_assistant_macro_is_escape_hatch_for_literal_macro_prose():
    # A line that should read as prose but looks like a USER macro: prefix it
    # with ### ASSISTANT: so it doesn't open a user turn.
    text = "### ASSISTANT: ### USER: this is literal prose"
    messages, prefill = parse_doc_macros(text)
    assert prefill == "### USER: this is literal prose"
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
    ]


def test_empty_macro_content_is_ignored():
    # An empty ### USER: drops out, so the prose on either side stays one block.
    text = "line one\n### USER:\nline two"
    messages, prefill = parse_doc_macros(text)
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
    ]
    assert prefill == "line one\nline two"


def test_system_lines_hoist_and_join_wherever_they_appear():
    # Trailing note makes "body/more body" a *closed* assistant turn (not the
    # prefill), so we can assert the mid-document SYSTEM line didn't split it.
    text = "### SYSTEM: rule one\n### USER: do it\nbody\n### SYSTEM: rule two\nmore body\n### USER: finish"
    messages, prefill = parse_doc_macros(text)
    assert messages == [
        {"role": "system", "content": "rule one\nrule two"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "body\nmore body"},
        {"role": "user", "content": "finish"},
    ]
    assert prefill is None


def test_case_insensitive_macros():
    messages, prefill = parse_doc_macros("### system: S\n### User: U\nbody")
    assert messages[0]["content"] == "S"
    assert messages[1] == {"role": "user", "content": "U"}
    assert prefill == "body"


def test_empty_document_yields_default_shape_none_prefill():
    messages, prefill = parse_doc_macros("")
    assert messages == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": _DEFAULT_USER},
    ]
    assert prefill is None


def test_alternation_invariant_holds_on_adversarial_interleavings():
    for text in [
        "a\n### USER: n1\nb\n### USER: n2\nc\n### USER: n3\nd",
        "### USER: n1\n### USER: n2\nprose\n### ASSISTANT: x\n### USER: n3\ntail",
        "prose only",
        "### USER: lonely note",
        "\n\n### USER: note after blanks\n\nprose\n\n",
        "### SYSTEM: s\n### SYSTEM: s2\n### USER: u\nbody\n### USER: u2",
    ]:
        messages, prefill = parse_doc_macros(text)
        _assert_alternates(messages, prefill)


# ── DocumentContinuer transport × mode branches ──────────────────────────────


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


async def test_text_assisted_calls_complete_with_parsed_messages_and_prefill():
    client = _StubClient("text")
    cont = DocumentContinuer(client, {"max_tokens": 512})
    text = "### USER: be vivid\nThe old lighthouse"
    out = await _drain(cont.stream(text, "m", assisted=True))

    assert out == ["chat-out"]  # reasoning delta dropped
    assert not client.raw_calls  # assisted goes through complete(), not complete_raw
    call = client.chat_calls[0]
    assert call["messages"] == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": "be vivid"},
    ]
    # Final prose is the open prefill; reasoning suppressed (no-op on text/prefill).
    assert call["params"]["prefill"] == "The old lighthouse"
    assert call["params"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}
    assert call["params"]["max_tokens"] == 512


async def test_text_assisted_trailing_note_passes_none_prefill():
    client = _StubClient("text")
    cont = DocumentContinuer(client, {})
    out = await _drain(cont.stream("prose\n### USER: write the ending", "m", assisted=True))

    assert out == ["chat-out"]
    call = client.chat_calls[0]
    # prefill=None → client falls through to the generation-prompt branch;
    # reasoning kwargs still sent (load-bearing for the trailing-note case).
    assert call["params"]["prefill"] is None
    assert call["messages"][-1] == {"role": "user", "content": "write the ending"}
    assert call["params"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


async def test_chat_assisted_closes_prefill_and_appends_reanchor_turn():
    client = _StubClient("chat")
    cont = DocumentContinuer(client, {})
    out = await _drain(cont.stream("### USER: be brief\nThe story so far", "m", assisted=True))

    assert out == ["chat-out"]
    call = client.chat_calls[0]
    # Chat transport can't hold an open prefill: close it + re-anchor with a user turn.
    assert call["messages"] == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": "be brief"},
        {"role": "assistant", "content": "The story so far"},
        {"role": "user", "content": DOC_ASSIST_CONTINUE},
    ]
    # No open prefill on the chat path.
    assert "prefill" not in call["params"]
    assert call["params"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


async def test_chat_assisted_trailing_note_sends_messages_as_is():
    client = _StubClient("chat")
    cont = DocumentContinuer(client, {})
    await _drain(cont.stream("prose\n### USER: wrap it up", "m", assisted=True))

    call = client.chat_calls[0]
    # prefill is None → no closed-prefill/re-anchor turns; messages end on the note.
    assert call["messages"][-1] == {"role": "user", "content": "wrap it up"}
    assert not any(m["content"] == DOC_ASSIST_CONTINUE for m in call["messages"])
