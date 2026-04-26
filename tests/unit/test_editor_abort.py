"""
Regression test: when an Editor ReAct iteration fails, the exception must
propagate out of editor_pass immediately.  No further LLM calls are made and
no synthetic 'done' event is yielded.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.llm_client import LLMClient
from backend.passes.editor.audit import AuditReport
from backend.passes.editor.editor import editor_pass
from backend.passes.editor.opening_monotony import MonotonyResult
from backend.passes.editor.slop_detector import (
    DetectionResult,
    FlaggedSentence,
    ClicheHit,
)
from backend.passes.editor.template_repetition import TemplateResult


def _make_client() -> LLMClient:
    return LLMClient("http://localhost:9999")


def _flagged_sentence(text: str, canonical: str, variant: str | None = None):
    return FlaggedSentence(
        sentence=text,
        cliches=[
            ClicheHit(canonical=canonical, variant=variant or canonical, score=1.0)
        ],
    )


def _make_report(issue_count: int) -> AuditReport:
    """Return an AuditReport with *issue_count* cliché hits."""
    flagged = [
        _flagged_sentence(f"Sentence {i}.", f"cliche-{i}") for i in range(issue_count)
    ]
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


@pytest.mark.asyncio
async def test_editor_iteration_exception_propagates():
    """If client.complete raises during iteration 2, the exception must escape
    editor_pass immediately — no 'done' event is yielded and no further LLM
    calls are attempted."""
    client = _make_client()

    llm_call_count = 0

    async def fake_complete(*args, **kwargs):
        nonlocal llm_call_count
        llm_call_count += 1
        if llm_call_count == 1:
            # First iteration: return a patch that fixes one of two issues
            yield {
                "type": "done",
                "message": {
                    "tool_calls": [
                        {
                            "id": "tc1",
                            "function": {
                                "name": "editor_apply_patch",
                                "arguments": json.dumps(
                                    {
                                        "patches": [
                                            {
                                                "search": "Sentence 0.",
                                                "replace": "Fixed 0.",
                                            }
                                        ]
                                    }
                                ),
                            },
                        }
                    ],
                    "content": "",
                },
            }
        else:
            # Second iteration: simulate an LLM API failure
            raise RuntimeError("LLM API exploded")

    client.complete = fake_complete

    settings = {
        "model_name": "test-model",
        "enable_agent": 1,
        "enabled_tools": {"editor_apply_patch": True},
        "reasoning_enabled_passes": {},
    }

    audit_call_count = 0

    def fake_run_contextual_audit(draft, phrase_bank, prev_msgs):
        nonlocal audit_call_count
        audit_call_count += 1
        if audit_call_count == 1:
            # Initial audit: 2 issues so the loop starts
            return _make_report(2), "audit text"
        if audit_call_count == 2:
            # Post-patch audit: 1 issue (progress made, so loop continues)
            return _make_report(1), "audit text"
        # Any further calls mean the loop kept running after the LLM failure
        pytest.fail(
            f"_run_contextual_audit called {audit_call_count} times; "
            "iteration should have aborted after LLM failure"
        )

    with patch(
        "backend.passes.editor.editor._run_contextual_audit",
        new=fake_run_contextual_audit,
    ):
        events = []
        with pytest.raises(RuntimeError, match="LLM API exploded"):
            async for event in editor_pass(
                client,
                prefix=[{"role": "system", "content": "sys"}],
                effective_msg="user msg",
                draft="Sentence 0. Sentence 1.",
                settings=settings,
                phrase_bank=[[]],
                audit_enabled=True,
                length_guard=None,
                enabled_tools={"editor_apply_patch": True},
            ):
                events.append(event)

    # The first iteration succeeded, so we called the LLM twice:
    # once for iteration 1, once for iteration 2 (which exploded).
    assert llm_call_count == 2

    # editor_pass only yields reasoning deltas and the final "done" dict.
    # fake_complete produced no reasoning events, and the final "done"
    # must NOT be yielded because the generator aborted mid-loop.
    assert len(events) == 0
