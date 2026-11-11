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

    def __init__(self, steps=()):
        self.replies = [reply for name, args in steps for reply in (("assistant_step", {"tool": name}), (name, args))]
        self.requests: list[tuple[list[dict], int]] = []

    async def complete(self, *, messages, cache_prefix_len, **_):
        self.requests.append(([dict(message) for message in messages], cache_prefix_len))
        name, args = self.replies.pop(0) if self.replies else ("assistant_step", {"tool": "reply"})
        if name == "assistant_step":
            args = {"message": "Done.", **args}
        call = {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
        yield {"type": "done", "message": {"content": "", "tool_calls": [call]}}


async def _run(client, history, request="And now?", result_chars=0):
    async def read(args):
        return ToolOutput({"text": args.name * result_chars}, f"Read {args.name}.")

    tools = [AssistantTool("read", "Read a thing.", _Read, "read", read)]
    events = [
        event
        async for event in run_assistant(
            client, "m", settings=Settings({}), tools=tools, history=history, request=request, view=VIEW, single_call=False
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
