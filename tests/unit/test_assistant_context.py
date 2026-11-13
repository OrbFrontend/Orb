"""The assistant's working context keeps the prefix a server has cached: what is rewritten, and how often."""

import json

from backend.core.settings import Settings
from backend.features.assistant import AssistantTool, HistoryItem, ToolOutput, ViewContext, run_assistant
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


async def _run(client, history, request="And now?", result_chars=0, model="m", **options):
    async def read(args):
        return ToolOutput({"text": args.name * result_chars}, f"Read {args.name}.")

    tools = [AssistantTool("read", "Read a thing.", _Read, "read", read)]
    events = [
        event
        async for event in run_assistant(
            client,
            model,
            settings=Settings({}),
            tools=tools,
            history=history,
            request=request,
            view=VIEW,
            single_call=False,
            **options,
        )
    ]
    return events[-1]


def _transcript(requests):
    items = []
    for index in range(requests):
        items += [
            HistoryItem("user", f"Question {index}? " + "q" * 380),
            HistoryItem("assistant", f"Answer {index}. " + "a" * 380),
        ]
    return items


def _shared(first, second):
    """How many leading messages two requests share byte for byte."""
    count = 0
    for a, b in zip(first, second, strict=False):
        if a != b:
            break
        count += 1
    return count


async def test_a_history_past_the_budget_keeps_its_start_from_one_request_to_the_next():
    # 800 characters a request, so the budget holds about 25 of them and every later request must drop some.
    starts = []
    for requests in range(30, 42):
        client = _Client()
        await _run(client, _transcript(requests))
        messages, prefix_len = client.requests[0]
        assert messages[1]["role"] == "user" and sum(len(m["content"]) for m in messages[1:]) <= CONTEXT_BUDGET_CHARS
        # The cache anchor sits after the replayed history, before the request that never repeats.
        assert prefix_len == len(messages) - 1
        starts.append(messages[1]["content"][:12])
    # Twelve requests added 9,600 characters; the start moved in 5,000-character steps, not once per request.
    assert len(set(starts)) <= 3


async def test_results_past_the_budget_are_shortened_together_and_later_calls_only_append():
    client = _Client([("read", {"name": letter}) for letter in "abcdefgh"])
    done = await _run(client, [], result_chars=5000)
    assert done == {"type": "done", "reason": "reply", "message": ""}
    sent = [messages for messages, _ in client.requests]
    rewrites = sum(_shared(before, after) < len(before) for before, after in zip(sent, sent[1:], strict=False))
    # Eight 5,000-character results against a 20,000 budget: shortening one result per step would rewrite on five steps.
    assert rewrites <= 3
    assert all(sum(len(m.get("content") or "") for m in messages[1:]) <= CONTEXT_BUDGET_CHARS for messages in sent)
    # The newest result is always whole.
    assert len(sent[-1][-1]["content"]) > 5000


async def test_a_follow_up_extends_the_previous_reply_byte_for_byte_and_anything_else_replays_history():
    kept = []
    first = _Client([("read", {"name": "a"})])
    await _run(first, [], request="What is a?", result_chars=300, keep=kept.append)
    (continuation,) = kept
    last_sent, _ = first.requests[-1]
    # Everything the reply was made from, then that reply's call and its one result.
    assert list(continuation.messages[: len(last_sent) - 1]) == last_sent[1:]
    assert [m["role"] for m in continuation.messages[len(last_sent) - 1 :]] == ["assistant", "tool"]

    history = [HistoryItem("user", "What is a?"), HistoryItem("assistant", "Done.")]
    follow = _Client()
    await _run(follow, history, request="And b?", resume=continuation, unchanged=True)
    messages, prefix_len = follow.requests[0]
    assert messages[: len(last_sent)] == last_sent
    assert messages[1:prefix_len] == list(continuation.messages)
    # The previous result is still whole, so nothing needs reading again.
    assert any(m["role"] == "tool" and "a" * 300 in m["content"] for m in messages[:prefix_len])
    unchanged_request = messages[-1]["content"]

    changed = _Client()
    await _run(changed, history, request="And b?", resume=continuation, unchanged=False)
    assert changed.requests[0][0][:-1] == messages[:-1] and changed.requests[0][0][-1]["content"] != unchanged_request

    # A different model, tool set or prompt cannot extend it: the browser's history replays instead.
    other = _Client()
    await _run(other, history, request="And b?", model="other", resume=continuation, unchanged=True)
    replayed, _ = other.requests[0]
    assert not any(m["role"] == "tool" for m in replayed) and replayed[1]["content"] == "What is a?"


async def test_thinking_keeps_its_reasoning_into_a_follow_up_and_a_run_without_it_sends_none_back():
    kept = []
    first = _Client([("read", {"name": "a"})], thought="Read a first.")
    await _run(first, [], request="What is a?", keep=kept.append, reasoning=True)
    # The template is asked to keep reasoning on earlier turns, so the next request renders the same bytes.
    assert all(params["chat_template_kwargs"]["preserve_thinking"] for params in first.params)
    (continuation,) = kept

    def carried(messages):
        return [m["reasoning_content"] for m in messages if "reasoning_content" in m]

    assert carried(continuation.messages) == ["Read a first."] * 3

    thinking = _Client()
    await _run(thinking, [], request="And b?", resume=continuation, reasoning=True)
    assert carried(thinking.requests[0][0]) == ["Read a first."] * 3

    plain = _Client()
    await _run(plain, [], request="And b?", resume=continuation)
    messages, prefix_len = plain.requests[0]
    assert carried(messages) == [] and prefix_len == len(continuation.messages) + 1
    assert plain.params[0]["chat_template_kwargs"]["enable_thinking"] is False


async def test_a_follow_up_is_told_how_its_held_reads_differ_and_a_repeated_read_sends_no_second_body():
    rows = [{"id": 1, "text": "one " * 50}, {"id": 2, "text": "two " * 50}]

    async def listing(args):
        return ToolOutput({"rows": list(rows)}, "Listed.")

    async def run(client, **options):
        tools = [AssistantTool("read", "List the rows.", _Read, "read", listing)]
        stream = run_assistant(
            client,
            "m",
            settings=Settings({}),
            tools=tools,
            history=[],
            request="Rows?",
            view=VIEW,
            single_call=False,
            **options,
        )
        return [event async for event in stream]

    kept = []
    await run(_Client([("read", {"name": "a"})]), keep=kept.append)
    rows.append({"id": 3, "text": "three " * 50})
    follow = _Client([("read", {"name": "a"}), ("read", {"name": "a"})])
    await run(follow, resume=kept[0], unchanged=False)
    request = follow.requests[0][0][-1]["content"]
    assert "three" in request and "one one" not in request
    first, second = [m["content"] for m in follow.requests[-1][0] if m["role"] == "tool" and m["content"][0] == "{"][-2:]
    assert "three" in first and "three" not in second and "one one" not in second
