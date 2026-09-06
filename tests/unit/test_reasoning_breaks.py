"""Tests for separating reasoning across completion calls."""

from __future__ import annotations

import json
from unittest.mock import patch

from backend.analysis import AuditReport, build_targets
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import (
    ClicheHit,
    DetectionResult,
    FlaggedSentence,
)
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.core import (
    ReasoningChannel,
    joined_delta,
    mark_call_start,
    reasoning_delta_event,
)
from backend.inference import CachedBase, LLMClient
from backend.pipeline.passes.editor.editor import editor_pass
from backend.pipeline.state import TurnState
from backend.prompting.tool_catalog import enabled_schemas


def _start(delta: str) -> dict:
    return {"type": "reasoning", "delta": delta, "call_start": True}


def _mid(delta: str) -> dict:
    return {"type": "reasoning", "delta": delta}


def test_first_call_starts_flush_and_mid_call_deltas_are_untouched():
    assert joined_delta("", _start("Evaluating")) == "Evaluating"
    assert joined_delta("Evaluating", _mid(" the exchange.")) == " the exchange."


def test_a_later_call_opens_a_blank_line():
    assert joined_delta("...for the lorebook.", _start("Recording")) == "\n\nRecording"


def test_newlines_at_the_seam_count_toward_the_blank_line():
    # Existing newlines count toward the separator.
    assert joined_delta("thought.\n", _start("next")) == "\nnext"
    assert joined_delta("thought.\n\n", _start("next")) == "next"
    assert joined_delta("thought.", _start("\nnext")) == "\n\nnext"
    assert joined_delta("thought.", _start("\n\n\nnext")) == "\n\n\nnext"


def test_channel_accumulates_what_it_streams():
    channel = ReasoningChannel()
    streamed = "".join(channel.push(ev) for ev in (_start("A."), _mid(" More."), _start("B.")))
    assert channel.text == streamed == "A. More.\n\nB."


def test_turn_state_buffers_every_pass_by_name():
    state = TurnState()
    assert state.add_reasoning("editor", _start("Auditing.")) == "Auditing."
    assert state.add_reasoning("editor", _start("Patching.")) == "\n\nPatching."
    # Each pass has its own buffer.
    assert state.add_reasoning("director", _start("Directing.")) == "Directing."
    assert state.reasoning_editor == "Auditing.\n\nPatching."
    assert state.reasoning_director == "Directing."
    assert state.reasoning_writer == ""


async def test_mark_call_start_flags_only_the_first_reasoning_delta():
    async def one_call():
        yield {"type": "reasoning", "delta": "a"}
        yield {"type": "content", "delta": "x"}
        yield {"type": "reasoning", "delta": "b"}
        yield {"type": "done", "message": {}}

    events = [ev async for ev in mark_call_start(one_call())]

    assert [ev.get("call_start", False) for ev in events] == [True, False, False, False]
    assert [ev["type"] for ev in events] == ["reasoning", "content", "reasoning", "done"]


async def test_mark_call_start_does_not_mutate_the_provider_event():
    source = {"type": "reasoning", "delta": "a"}

    async def one_call():
        yield source

    [event] = [ev async for ev in mark_call_start(one_call())]

    assert event["call_start"] is True
    assert "call_start" not in source


def test_relays_rebuild_the_event_without_dropping_the_seam():
    assert reasoning_delta_event(_start("a")) == {"type": "reasoning", "delta": "a", "call_start": True}
    assert reasoning_delta_event(_mid("a")) == {"type": "reasoning", "delta": "a"}


SETTINGS = {
    "model_name": "test-model",
    "enable_agent": 1,
    "enabled_tools": {"editor_apply_patch": True},
    "reasoning_enabled_passes": {"editor": True},
}


def _report(sentences: list[str]) -> AuditReport:
    flagged = [
        FlaggedSentence(sentence=s, cliches=[ClicheHit(phrase=f"cliche-{i}", score=1.0)]) for i, s in enumerate(sentences)
    ]
    return AuditReport(
        cliche_result=DetectionResult(
            flagged_sentences=flagged,
            unique_cliches=[f"cliche-{i}" for i in range(len(sentences))],
            total_sentences=max(1, len(sentences)),
            flagged_count=len(sentences),
        ),
        monotony_result=MonotonyResult([], {}, 0, 0.0),
        template_result=TemplateResult([], {}, 0, 0, 0.0),
        not_but_result=[],
        structural_repetition_result=None,
    )


async def test_editor_iterations_land_in_the_buffer_as_separate_paragraphs():
    """Reasoning from two editor iterations stays separated in one buffer."""
    client = LLMClient("http://localhost:9999")
    thoughts = iter(["Sentence 0 is the worst.", "Still two left."])

    async def fake_complete(*args, **kwargs):
        for delta in next(thoughts).split(" "):
            yield {"type": "reasoning", "delta": delta + " "}
        yield {
            "type": "done",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {
                            "name": "editor_apply_patch",
                            "arguments": json.dumps({"patches": [{"id": 1, "replace": "Fixed."}]}),
                        },
                    }
                ],
            },
        }

    client.complete = fake_complete
    # 3 issues → 2 issues → clean, producing two editor calls.
    audits = iter(
        [
            _report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
            _report(["Sentence 1.", "Sentence 2."]),
            _report([]),
        ]
    )

    async def fake_audit(draft, phrase_bank, prev_msgs, audit_toggles=None, user_message=""):
        report = next(audits)
        return report, build_targets(report, draft)

    state = TurnState()
    with patch("backend.pipeline.passes.editor.editor._run_contextual_audit", new=fake_audit):
        async for event in editor_pass(
            client,
            CachedBase(
                prefix=({"role": "system", "content": "sys"},),
                tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})),
                model="test-model",
            ),
            effective_msg="user msg",
            draft="Sentence 0. Sentence 1. Sentence 2.",
            settings=SETTINGS,
            phrase_bank=[[]],
            audit_enabled=True,
            length_guard=None,
            reasoning_on=True,
        ):
            if event["type"] == "reasoning":
                state.add_reasoning("editor", event)

    assert state.reasoning_editor == "Sentence 0 is the worst. \n\nStill two left. "
