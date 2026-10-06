"""Judge gates on post-processing fragments: verdicts, fail-open reasons, budget, Stop."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from backend.database.queries.character_cards import card_embedded_fragments
from backend.inference import (
    MAX_QUESTION_BYTES,
    MAX_STATE_BYTES,
    CachedBase,
    DecisionClient,
    DecisionResponse,
    DecisionTransportError,
    LLMClient,
)
from backend.pipeline.passes.editor import editor_pass, post_processing_step
from backend.pipeline.passes.editor import post_processing as post_processing_module
from backend.pipeline.passes.editor.gate import GATE_CRITERIA, gate_state, judge_gate
from backend.pipeline.passes.judge import JudgeConfig
from backend.prompting.tool_catalog import enabled_schemas

CONFIG = JudgeConfig(url="https://judge.test/alpha/decisions", api_key="k", model="typesafe/jev-1.13")
SETTINGS = {"model_name": "test-model", "enable_agent": 1, "reasoning_enabled_passes": {}}
REQUEST = "Mara waits at the tavern."


def _fragment(fid: str, gate: str = "", sort_order: int = 0, replies: int = 0) -> dict:
    return {
        "id": fid,
        "label": fid.title(),
        "injection_label": fid.title(),
        "description": f"Instruction for {fid}",
        "field_type": "post_processing",
        "sort_order": sort_order,
        "post_processing_gate": gate,
        "post_processing_gate_replies": replies,
    }


def _base() -> CachedBase:
    return CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas({"editor_search_replace": True}, {})),
        model="test-model",
    )


class Editor:
    """An LLMClient whose Editor calls apply scripted exact patches."""

    def __init__(self, patches: list[tuple[str, str]] | None = None, *, on_call=None):
        self.client = LLMClient("http://localhost:9999")
        self.patches = list(patches or [])
        self.seen: list[list[dict]] = []
        self.on_call = on_call
        editor = self

        async def complete(*, messages, **kwargs):
            editor.seen.append(list(messages))
            if editor.on_call is not None:
                editor.on_call()
            search, replace = editor.patches.pop(0) if editor.patches else ("", "")
            arguments = json.dumps({"patches": [{"search": search, "replace": replace}]})
            yield {
                "type": "done",
                "message": {
                    "content": "",
                    "tool_calls": [{"id": "c", "function": {"name": "editor_search_replace", "arguments": arguments}}],
                },
            }

        self.client.complete = complete  # type: ignore[method-assign]

    @property
    def drafts(self) -> list[str]:
        """The draft each Editor call was asked to edit."""
        return [messages[-2]["content"] for messages in self.seen]


class Judge:
    """A stubbed ``DecisionClient.decide`` answering per fragment id."""

    def __init__(self, monkeypatch, answers: dict[str, object] | None = None, *, error: Exception | None = None):
        self.answers = answers or {}
        self.error = error
        self.states: list[str] = []
        self.questions: list[dict] = []
        self.timeouts: list[float | None] = []
        self.before_answer = None
        judge = self

        async def decide(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
            judge.states.append(state)
            judge.questions.extend(question.payload() for question in questions)
            judge.timeouts.append(timeout)
            if judge.before_answer is not None:
                judge.before_answer()
            if judge.error is not None:
                raise judge.error
            answers = {q.key: judge.answers[q.key] for q in questions if q.key in judge.answers}
            return DecisionResponse(answers=answers)  # type: ignore[arg-type]

        monkeypatch.setattr(DecisionClient, "decide", decide)


async def _step(editor: Editor, fragments: list[dict], draft: str = "Hello there.", *, config=CONFIG, msg=REQUEST):
    events = [
        event
        async for event in post_processing_step(
            editor.client,
            _base(),
            draft,
            SETTINGS,
            fragments,
            writer_user_msg=f"___\n\n{msg}",
            effective_msg=msg,
            judge_config=config,
        )
    ]
    return events, events[-1]["result"]


def _gates(result) -> list[dict]:
    return [call["arguments"] for call in result.tool_calls if call["name"] == "post_processing_gate"]


# Verdicts


@pytest.mark.parametrize(
    ("probability", "fired", "reason"),
    [(0.9, 1, "condition_met"), (0.5, 1, "condition_met"), (0.49, 0, "condition_not_met"), (0.1, 0, "condition_not_met")],
)
async def test_a_yes_runs_the_fragment_and_a_no_skips_it(monkeypatch, probability, fired, reason):
    Judge(monkeypatch, {"trim": probability})
    editor = Editor([("Hello", "Hey")])

    events, result = await _step(editor, [_fragment("trim", "Is it long?")])

    assert _gates(result) == [
        {
            "fragment_id": "trim",
            "label": "Trim",
            "question": "Is it long?",
            "fired": fired,
            "reason": reason,
            "probability": probability,
        }
    ]
    assert len(editor.seen) == fired
    assert result.draft == ("Hey there." if fired else "Hello there.")
    assert any(event["type"] == "draft_update" for event in events) is bool(fired)


async def test_the_judge_sees_the_fixed_template_and_criteria(monkeypatch):
    judge = Judge(monkeypatch, {"trim": 0.9})

    await _step(Editor(), [_fragment("trim", "  Is it long?\n")])

    assert judge.states == [f"Current request:\n{REQUEST}\n\nReply:\nHello there."]
    assert judge.questions == [{"type": "noul", "instructions": "Is it long?", "criteria": dict(GATE_CRITERIA)}]


@pytest.mark.parametrize("gate", ["", "   \n\t"])
async def test_a_blank_gate_runs_without_asking_or_recording(monkeypatch, gate):
    judge = Judge(monkeypatch, {"trim": 0.1})
    editor = Editor([("Hello", "Hey")])

    _, result = await _step(editor, [_fragment("trim", gate)])

    assert judge.states == []
    assert _gates(result) == []
    assert result.draft == "Hey there."


async def test_a_fragment_without_the_gate_key_runs_ungated(monkeypatch):
    judge = Judge(monkeypatch)
    fragment = _fragment("trim")
    del fragment["post_processing_gate"]

    _, result = await _step(Editor([("Hello", "Hey")]), [fragment])

    assert judge.states == [] and result.draft == "Hey there."


# Fail-open


@pytest.mark.parametrize("config", [None, JudgeConfig()])
async def test_an_unconfigured_judge_fails_open(monkeypatch, config):
    judge = Judge(monkeypatch, {"trim": 0.1})
    editor = Editor([("Hello", "Hey")])

    _, result = await _step(editor, [_fragment("trim", "Is it long?")], config=config)

    assert judge.states == []
    assert _gates(result)[0]["fired"] == 1 and _gates(result)[0]["reason"] == "not_configured"
    assert "probability" not in _gates(result)[0]
    assert result.draft == "Hey there."


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "transport_failure"),
        (DecisionTransportError("bad json"), "transport_failure"),
    ],
)
async def test_transport_errors_fail_open(monkeypatch, error, reason):
    Judge(monkeypatch, error=error)
    editor = Editor([("Hello", "Hey")])

    _, result = await _step(editor, [_fragment("trim", "Is it long?")])

    assert _gates(result)[0]["fired"] == 1 and _gates(result)[0]["reason"] == reason
    assert result.draft == "Hey there."


async def test_a_provider_error_fails_open(monkeypatch):
    from backend.inference.errors import llm_call_error

    request = httpx.Request("POST", CONFIG.url)
    response = httpx.Response(502, request=request, text="bad gateway")
    Judge(monkeypatch, error=llm_call_error(response=response, body="bad gateway", url=CONFIG.url, model="m", api_key="k"))

    _, result = await _step(Editor(), [_fragment("trim", "Is it long?")])

    assert _gates(result)[0]["reason"] == "transport_failure" and _gates(result)[0]["fired"] == 1


@pytest.mark.parametrize("answers", [{}, {"other": 0.1}])
async def test_a_missing_answer_fails_open(monkeypatch, answers):
    Judge(monkeypatch, answers)

    _, result = await _step(Editor(), [_fragment("trim", "Is it long?")])

    assert _gates(result)[0]["reason"] == "invalid_answer" and _gates(result)[0]["fired"] == 1


async def test_a_programming_error_is_not_swallowed(monkeypatch):
    Judge(monkeypatch, error=KeyError("bug"))

    with pytest.raises(KeyError):
        await _step(Editor(), [_fragment("trim", "Is it long?")])


def _draft_of_state_bytes(target: int) -> str:
    """A multi-byte draft whose gate state is exactly *target* UTF-8 bytes."""
    remaining = target - len(gate_state(REQUEST, "").encode())
    return "é" * (remaining // 2) + "a" * (remaining % 2)


async def test_the_state_limit_counts_utf8_bytes_inclusive(monkeypatch):
    judge = Judge(monkeypatch, {"trim": 0.9})
    at_limit = _draft_of_state_bytes(MAX_STATE_BYTES)
    assert len(gate_state(REQUEST, at_limit)) < MAX_STATE_BYTES  # characters alone would pass

    ok = await judge_gate(CONFIG, _fragment("trim", "Q?"), effective_msg=REQUEST, draft=at_limit, timeout_seconds=3)
    over = await judge_gate(CONFIG, _fragment("trim", "Q?"), effective_msg=REQUEST, draft=at_limit + "a", timeout_seconds=3)

    assert ok["arguments"]["reason"] == "condition_met"
    assert len(judge.states) == 1
    assert over["arguments"] == {
        "fragment_id": "trim",
        "label": "Trim",
        "question": "Q?",
        "fired": 1,
        "reason": "oversized_input",
        "oversize_state_bytes": MAX_STATE_BYTES + 1,
        "oversize_question_bytes": len(b"Q?") + sum(len(text.encode()) for text in GATE_CRITERIA.values()),
        "state_limit": MAX_STATE_BYTES,
        "question_limit": MAX_QUESTION_BYTES,
    }


async def test_the_question_limit_includes_the_criteria(monkeypatch):
    judge = Judge(monkeypatch, {"trim": 0.9})
    room = MAX_QUESTION_BYTES - sum(len(text.encode()) for text in GATE_CRITERIA.values())
    at_limit = "ü" * (room // 2) + "?" * (room % 2)

    ok = await judge_gate(CONFIG, _fragment("trim", at_limit), effective_msg=REQUEST, draft="Hi.", timeout_seconds=3)
    over = await judge_gate(CONFIG, _fragment("trim", at_limit + "?"), effective_msg=REQUEST, draft="Hi.", timeout_seconds=3)

    assert ok["arguments"]["reason"] == "condition_met" and len(judge.states) == 1
    assert over["arguments"]["reason"] == "oversized_input" and over["arguments"]["fired"] == 1
    assert over["arguments"]["oversize_question_bytes"] == MAX_QUESTION_BYTES + 1


async def test_an_oversized_imported_card_gate_fails_open_without_truncation(monkeypatch):
    judge = Judge(monkeypatch, {"huge": 0.1})
    gate = "Is the reply long? " * 1000  # far past both the API cap and the Judge limit
    card = {
        "extensions": {
            "orb": {
                "fragments": {
                    "interactive": [
                        {"id": "huge", "label": "Huge", "field_type": "post_processing", "post_processing_gate": gate}
                    ]
                }
            }
        }
    }
    _, [fragment] = card_embedded_fragments(card)
    assert fragment["post_processing_gate"] == gate

    _, result = await _step(Editor([("Hello", "Hey")]), [dict(fragment)])

    assert judge.states == []
    assert _gates(result)[0]["reason"] == "oversized_input"
    assert _gates(result)[0]["question"] == gate.strip()
    assert result.draft == "Hey there."


# Draft and request threading


@pytest.mark.parametrize(("replies", "shown"), [(0, []), (2, ["Second.", "Third."]), (10, ["First.", "Second.", "Third."])])
async def test_the_judge_reads_the_asked_for_previous_replies_oldest_first(monkeypatch, replies, shown):
    judge = Judge(monkeypatch, {"trim": 0.9})

    record = await judge_gate(
        CONFIG,
        _fragment("trim", "Leak?", replies=replies),
        effective_msg=REQUEST,
        draft="Hi.",
        timeout_seconds=3,
        recent_replies=["Third.", "Second.", "First."],
    )

    assert judge.states == [gate_state(REQUEST, "Hi.", shown)]
    assert record["arguments"].get("previous_replies", 0) == len(shown)


@pytest.mark.parametrize("override", [None, ["Kept."]])
async def test_editor_pass_feeds_gates_the_same_replies_as_the_audit_window(monkeypatch, override):
    judge = Judge(monkeypatch, {"trim": 0.9})
    base = CachedBase(
        prefix=(
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "Older."},
            {"role": "user", "content": "Go on."},
            {"role": "assistant", "content": "Newer."},
        ),
        tools=_base().tools,
        model="test-model",
    )

    async for _ in editor_pass(
        Editor().client,
        base,
        REQUEST,
        "Hello there.",
        SETTINGS,
        [],
        audit_enabled=False,
        audit_context_msgs=override,
        post_processing_fragments=[_fragment("trim", "Leak?", replies=5)],
        judge_config=CONFIG,
    ):
        pass

    assert judge.states == [gate_state(REQUEST, "Hello there.", ["Kept."] if override else ["Older.", "Newer."])]


async def test_each_gate_sees_the_request_and_the_evolving_draft(monkeypatch):
    judge = Judge(monkeypatch, {"second": 0.9, "third": 0.9})
    editor = Editor([("Hello", "Hey"), ("Hey there.", "Hey, friend."), ("friend", "pal")])
    fragments = [
        _fragment("third", "Third?", sort_order=3),
        _fragment("first", sort_order=1),  # ungated, and its edit prompt must not shadow the request
        _fragment("second", "Second?", sort_order=2),
    ]

    _, result = await _step(editor, fragments)

    assert judge.states == [gate_state(REQUEST, "Hey there."), gate_state(REQUEST, "Hey, friend.")]
    assert editor.drafts == ["Hello there.", "Hey there.", "Hey, friend."]
    assert result.draft == "Hey, pal."
    assert [call["name"] for call in result.tool_calls] == [
        "editor_search_replace",
        "post_processing_gate",
        "editor_search_replace",
        "post_processing_gate",
        "editor_search_replace",
    ]


async def test_a_skipped_fragment_leaves_the_draft_for_the_next_one(monkeypatch):
    Judge(monkeypatch, {"first": 0.1, "second": 0.9})
    editor = Editor([("Hello", "Hey")])

    events, result = await _step(editor, [_fragment("first", "A?", 1), _fragment("second", "B?", 2)])

    assert editor.drafts == ["Hello there."]
    assert [call["name"] for call in result.tool_calls] == [
        "post_processing_gate",
        "post_processing_gate",
        "editor_search_replace",
    ]
    assert [event["draft"] for event in events if event["type"] == "draft_update"] == ["Hey there."]


# Judge-wait budget


async def test_judge_waiting_spends_one_shared_allowance_and_editing_does_not(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(post_processing_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    judge = Judge(monkeypatch, {f"f{i}": 0.9 for i in range(5)})

    def judge_wait():
        clock.now += 2.4

    def editor_work():
        clock.now += 100.0  # would exhaust any budget it were charged to

    judge.before_answer = judge_wait
    editor = Editor(on_call=editor_work)
    fragments = [_fragment(f"f{i}", f"Q{i}?", sort_order=i) for i in range(5)]

    _, result = await _step(editor, fragments)

    assert judge.timeouts == [pytest.approx(6.0), pytest.approx(3.6), pytest.approx(1.2)]
    assert [gate["reason"] for gate in _gates(result)] == [
        "condition_met",
        "condition_met",
        "condition_met",
        "budget_exhausted",
        "budget_exhausted",
    ]
    assert all(gate["fired"] == 1 for gate in _gates(result))
    assert len(editor.seen) == 5


async def test_blank_gates_spend_no_allowance(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(post_processing_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    judge = Judge(monkeypatch, {"gated": 0.9})
    editor = Editor(on_call=lambda: setattr(clock, "now", clock.now + 10.0))

    await _step(editor, [_fragment("blank", "", 1), _fragment("gated", "Q?", 2)])

    assert judge.timeouts == [pytest.approx(6.0)]


async def test_the_overall_deadline_bounds_a_stalled_judge(monkeypatch):
    stalled = asyncio.Event()

    async def _post(self, body, timeout):  # noqa: ANN001
        await stalled.wait()

    monkeypatch.setattr(DecisionClient, "_post", _post)

    record = await judge_gate(CONFIG, _fragment("trim", "Q?"), effective_msg=REQUEST, draft="Hi.", timeout_seconds=0.02)

    assert record["arguments"]["reason"] == "timeout" and record["arguments"]["fired"] == 1


async def test_no_allowance_sends_no_request(monkeypatch):
    judge = Judge(monkeypatch, {"trim": 0.1})

    record = await judge_gate(CONFIG, _fragment("trim", "Q?"), effective_msg=REQUEST, draft="Hi.", timeout_seconds=0)

    assert judge.states == []
    assert record["arguments"]["reason"] == "budget_exhausted" and record["arguments"]["fired"] == 1


# Stop


class PendingJudge:
    """A real ``decide`` over a transport that waits until released."""

    def __init__(self, monkeypatch, answer: float = 0.9):
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.requests = 0
        pending = self

        async def _post(self, body, timeout):  # noqa: ANN001
            pending.requests += 1
            key = next(iter(json.loads(body)["questions"]))
            pending.reached.set()
            try:
                await pending.release.wait()
            except asyncio.CancelledError:
                pending.cancelled = True
                raise
            return {"answers": {key: {"type": "noul", "noul": answer}}}

        monkeypatch.setattr(DecisionClient, "_post", _post)


async def test_stop_during_a_pending_gate_keeps_earlier_work_and_starts_nothing(monkeypatch):
    pending = PendingJudge(monkeypatch)
    editor = Editor([("Hello", "Hey"), ("Hey", "Oops")])
    feedback_calls: list[bool] = []

    async def feedback_step(*args, **kwargs):
        feedback_calls.append(True)
        yield {"type": "done", "result": SimpleNamespace(values={})}

    monkeypatch.setattr("backend.pipeline.passes.editor.editor.feedback_step", feedback_step)

    async def run() -> list[dict]:
        return [
            event
            async for event in editor_pass(
                editor.client,
                _base(),
                REQUEST,
                "Hello there.",
                SETTINGS,
                [],
                audit_enabled=False,
                post_processing_fragments=[
                    _fragment("first", sort_order=1),
                    _fragment("second", "Q?", sort_order=2),
                    _fragment("third", "Q?", sort_order=3),
                ],
                feedback_fragments=[{"id": "fb", "field_type": "feedback"}],
                judge_config=CONFIG,
            )
        ]

    task = asyncio.create_task(run())
    await asyncio.wait_for(pending.reached.wait(), 1)
    editor.client.abort()
    events = await asyncio.wait_for(task, 1)

    assert pending.cancelled and pending.requests == 1
    assert len(editor.seen) == 1
    assert feedback_calls == []
    done = events[-1]
    assert done["draft"] == "Hey there."
    assert [call["name"] for call in done["tool_calls"]] == ["editor_search_replace"]


async def test_stop_racing_a_completed_answer_skips_the_fragment(monkeypatch):
    judge = Judge(monkeypatch, {"first": 0.9, "second": 0.9})
    editor = Editor([("Hello", "Hey")])
    judge.before_answer = editor.client.abort

    _, result = await _step(editor, [_fragment("first", "Q?", 1), _fragment("second", "Q?", 2)])

    assert editor.seen == []
    assert len(judge.states) == 1
    assert _gates(result) == [
        {"fragment_id": "first", "label": "First", "question": "Q?", "fired": 1, "reason": "condition_met", "probability": 0.9}
    ]
    assert result.draft == "Hello there."


async def test_task_cancellation_is_not_turned_into_a_fail_open(monkeypatch):
    pending = PendingJudge(monkeypatch)
    task = asyncio.create_task(
        judge_gate(CONFIG, _fragment("trim", "Q?"), effective_msg=REQUEST, draft="Hi.", timeout_seconds=3)
    )
    await asyncio.wait_for(pending.reached.wait(), 1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending.cancelled
