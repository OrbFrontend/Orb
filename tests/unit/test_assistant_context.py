"""The assistant's conversation only grows: each request extends the last one whole, as a coding agent's session does."""

import json

from backend.core.settings import Settings
from backend.features.assistant import AssistantTool, ToolFailure, ToolOutput, ViewContext, run_assistant
from backend.features.assistant.contracts import CONTEXT_BUDGET_CHARS, ToolArguments

VIEW = ViewContext("library")


class _Read(ToolArguments):
    name: str


class _Client:
    """Records every request and answers from a script of ``(tool, arguments)`` steps, then replies."""

    base_url = "http://model.test/v1"
    is_aborted = False

    def __init__(self, steps=(), thought=""):
        self.replies = [reply for name, args in steps for reply in (("assistant_step", {"tool": name}), (name, args))]
        self.requests: list[tuple[list[dict], int]] = []
        self.params: list[dict] = []
        self.thought = thought

    def sends_tool_schemas(self, messages, model, **kwargs):
        return True

    async def complete(self, *, messages, cache_prefix_len, **params):
        self.requests.append(([dict(message) for message in messages], cache_prefix_len))
        self.params.append(params)
        name, args = self.replies.pop(0) if self.replies else ("assistant_step", {"tool": "reply"})
        if name == "assistant_step":
            args = {"message": "Done.", **args}
        call = {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        thought = {"reasoning_content": self.thought} if self.thought else {}
        yield {"type": "done", "message": {"content": "", "tool_calls": [call], **thought}}


async def _run(client, transcript=None, request="And now?", result_chars=0, model="m", **options):
    """Run one request over *transcript* and return ``(done, the conversation it left)``."""

    async def read(args):
        if args.name == "stop":
            client.is_aborted = True
            raise ToolFailure("Unavailable.")
        return ToolOutput({"text": args.name * result_chars}, f"Read {args.name}.")

    kept = []
    tools = [AssistantTool("read", "Read a thing.", _Read, "read", read)]
    events = [
        event
        async for event in run_assistant(
            client,
            model,
            settings=Settings({}),
            tools=tools,
            transcript=transcript,
            request=request,
            view=VIEW,
            single_call=False,
            keep=kept.append,
            **options,
        )
    ]
    (conversation,) = kept
    return events[-1], conversation


def _sent(client):
    return [messages for messages, _ in client.requests]


async def test_each_request_extends_the_last_whole_with_every_call_and_result_as_the_model_read_it():
    first = _Client([("read", {"name": "a"}), ("read", {"name": "b"})])
    done, conversation = await _run(first, request="What are a and b?", result_chars=5000)
    assert done == {"type": "done", "reason": "reply", "message": ""}
    sent = _sent(first)
    # Within the run every call only appends: nothing the model has read is shortened or replaced.
    assert all(after[: len(before)] == before for before, after in zip(sent, sent[1:], strict=False))
    # What is kept is what the last call was sent, then the reply's call and its one result.
    assert list(conversation.messages[: len(sent[-1]) - 1]) == sent[-1][1:]
    assert [m["role"] for m in conversation.messages[len(sent[-1]) - 1 :]] == ["assistant", "tool"]

    follow = _Client([("read", {"name": "c"})])
    _, longer = await _run(follow, conversation, request="And c?", result_chars=5000)
    messages, prefix_len = follow.requests[0]
    # The next request reads the same bytes, both results whole, and the cache anchor sits where the last request ended.
    assert messages[1:prefix_len] == list(conversation.messages) and prefix_len == len(messages) - 1
    assert sum(m["role"] == "tool" and len(m["content"]) > 5000 for m in messages) == 2
    assert messages[-1]["content"].startswith("And c?")
    assert longer.messages[: len(conversation.messages)] == conversation.messages


async def test_a_request_that_got_no_answer_stays_in_the_conversation_with_every_call_answered():
    stopped = _Client([("read", {"name": "a"}), ("read", {"name": "stop"})])
    done, conversation = await _run(stopped, request="Read a, then stop.", result_chars=100)
    assert done["reason"] == "stopped"
    calls = [call["id"] for m in conversation.messages for call in m.get("tool_calls") or ()]
    assert calls and calls == [m["tool_call_id"] for m in conversation.messages if m["role"] == "tool"]
    # It closes on the assistant's side, saying how it ended, so the next request is never read as part of it.
    assert conversation.messages[-1] == {"role": "assistant", "content": "The user stopped this request before it finished."}

    follow = _Client()
    await _run(follow, conversation, request="Go on.")
    messages, prefix_len = follow.requests[0]
    assert messages[1:prefix_len] == list(conversation.messages)
    assert [m["role"] for m in messages[-2:]] == ["assistant", "user"]


async def test_only_a_conversation_past_the_budget_loses_its_oldest_requests_whole_and_in_bulk():
    conversation, starts = None, []
    for index in range(12):
        client = _Client([("read", {"name": "r"}), ("read", {"name": "s"})])
        _, conversation = await _run(client, conversation, request=f"Question {index}?", result_chars=5500)
        for messages in _sent(client):
            assert (
                messages[1]["role"] == "user" and sum(len(m.get("content") or "") for m in messages[1:]) <= CONTEXT_BUDGET_CHARS
            )
        starts.append(_sent(client)[0][1]["content"][:11])
    assert starts[0] == starts[5] == "Question 0?", "under the budget nothing is dropped"
    # Twelve 11,000-character requests against an 80,000 budget: the start moves a quarter of the budget at a time.
    assert 1 < len(set(starts)) <= 4


async def test_thinking_keeps_its_reasoning_in_the_conversation_and_a_run_without_it_sends_none_back():
    first = _Client([("read", {"name": "a"})], thought="Read a first.")
    _, conversation = await _run(first, request="What is a?", reasoning=True)
    # The template is asked to keep reasoning on earlier turns, so the next request renders the same bytes.
    assert all(params["chat_template_kwargs"]["preserve_thinking"] for params in first.params)

    def carried(messages):
        return [m["reasoning_content"] for m in messages if "reasoning_content" in m]

    assert carried(conversation.messages) == ["Read a first."] * 3

    thinking = _Client()
    await _run(thinking, conversation, request="And b?", reasoning=True)
    assert carried(thinking.requests[0][0]) == ["Read a first."] * 3

    # Reasoning another model or endpoint made is not valid there, and a run with thinking off sends none.
    for options in ({"reasoning": True, "model": "other"}, {}):
        plain = _Client()
        await _run(plain, conversation, request="And b?", **options)
        messages, prefix_len = plain.requests[0]
        assert carried(messages) == [] and prefix_len == len(conversation.messages) + 1
    assert plain.params[0]["chat_template_kwargs"]["enable_thinking"] is False
