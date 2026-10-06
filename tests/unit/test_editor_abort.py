"""Regression test: when an Editor ReAct iteration fails, the loop stops with no further LLM calls, reports the failure, and its
'done' keeps the draft the finished iterations produced. The failure does not escape editor_pass.
"""

import json
from unittest.mock import patch

import pytest

from backend.analysis import AuditReport, build_targets
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import ClicheHit, DetectionResult, FlaggedSentence
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.inference import CachedBase, LLMClient
from backend.pipeline.passes.editor.editor import editor_pass
from backend.prompting.tool_catalog import enabled_schemas


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


def _flagged_sentence(text: str, phrase: str):
    return FlaggedSentence(sentence=text, cliches=[ClicheHit(phrase=phrase, score=1.0)])


def _make_report(issue_count: int) -> AuditReport:
    """Return an AuditReport with *issue_count* cliché hits."""
    flagged = [_flagged_sentence(f"Sentence {i}.", f"cliche-{i}") for i in range(issue_count)]
    return AuditReport(
        cliche_result=DetectionResult(
            flagged_sentences=flagged,
            unique_cliches=[f"cliche-{i}" for i in range(issue_count)],
            total_sentences=max(1, issue_count),
            flagged_count=issue_count,
        ),
        monotony_result=MonotonyResult([], {}, 0, 0.0),
        template_result=TemplateResult([], {}, 0, 0, 0.0),
        not_but_result=[],
        structural_repetition_result=None,
    )


def _call(n: int, name: str, arguments: dict) -> dict:
    tool_call = {"id": f"tc{n}", "function": {"name": name, "arguments": json.dumps(arguments)}}
    return {"type": "done", "message": {"tool_calls": [tool_call], "content": ""}}


_FIX_ONE = ("editor_apply_patch", {"patches": [{"id": 1, "replace": "Fixed 0."}]})


async def _edit(client: LLMClient, **kwargs) -> list[dict]:
    """Run editor_pass over two flagged sentences; each audit after the first reports one issue fewer, and a third fails."""
    audits = 0

    async def fake_run_contextual_audit(draft, phrase_bank, prev_msgs, audit_toggles=None, user_message=""):
        nonlocal audits
        audits += 1
        if audits > 2:
            pytest.fail("the loop audited again after it should have stopped")
        report = _make_report(4 - audits)
        return report, build_targets(report, draft)

    base = CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})),
        model="test-model",
    )
    with patch("backend.pipeline.passes.editor.editor._run_contextual_audit", new=fake_run_contextual_audit):
        return [
            event
            async for event in editor_pass(
                client,
                base,
                effective_msg="user msg",
                draft="Sentence 0. Sentence 1.",
                settings={"model_name": "test-model", "enabled_tools": {"editor_apply_patch": True}},
                phrase_bank=[[]],
                audit_enabled=True,
                length_guard=None,
                **kwargs,
            )
        ]


async def test_editor_iteration_failure_stops_the_loop_and_keeps_the_draft():
    """If client.complete raises during iteration 2, the loop reports it and stops with no further LLM calls."""
    client = _make_client()
    calls = 0

    async def fake_complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("LLM API exploded")
        yield _call(1, *_FIX_ONE)

    client.complete = fake_complete
    events = await _edit(client)

    assert calls == 2
    # Iteration 1's patch surfaces as a draft_update, the failure is reported, and "done" hands the patch back with the call
    # that produced it.
    assert [e["type"] for e in events] == ["step", "draft_update", "failure", "done"]
    assert events[1]["draft"] == "Fixed 0. Sentence 1."
    assert events[2]["during"] == "output_auditor"
    assert str(events[2]["error"]) == "LLM API exploded"
    assert events[3]["draft"] == "Fixed 0. Sentence 1."
    assert [call["name"] for call in events[3]["tool_calls"]] == ["editor_apply_patch"]


async def test_a_stop_mid_call_keeps_finished_patches_and_discards_the_cut_short_output():
    """Stop lands during iteration 2, whose response is whatever had streamed by then: a rewrite built from it is not an edit,
    so the draft stays iteration 1's and nothing further runs."""
    client = _make_client()
    calls = 0

    async def fake_complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            client.abort()
            yield _call(calls, "editor_rewrite", {"rewritten_text": "Half a rewr"})
        else:
            yield _call(calls, *_FIX_ONE)

    client.complete = fake_complete
    events = await _edit(client, feedback_fragments=[{"id": "mood", "label": "Mood"}])

    assert calls == 2, "no call may start after the stop"
    assert [e["type"] for e in events] == ["step", "draft_update", "done"]
    assert events[-1]["draft"] == "Fixed 0. Sentence 1."
