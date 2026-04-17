"""
test_refine_loop.py — Tests for the ReAct loop in editor_pass.

Verifies that:
  1. The loop terminates early when all issues are fixed (audit clean).
  2. The updated audit report is sent back to the model each iteration.
  3. The loop runs exactly MAX_EDITOR_ITERATIONS when issues decrease but never clear.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from backend.passes.refine.audit import AuditReport
from backend.passes.refine.slop_detector import (
    DetectionResult,
    FlaggedSentence,
    ClicheHit,
)
from backend.passes.refine.opening_monotony import MonotonyResult
from backend.passes.refine.template_repetition import TemplateResult
from backend.passes.refine.refine import editor_pass
from backend.tool_defs import MAX_EDITOR_ITERATIONS


# ── Helpers ───────────────────────────────────────────────────────────────────


def _dirty_report(n_issues: int, label: str = "") -> AuditReport:
    """Return an AuditReport with *n_issues* fake cliché hits."""
    flagged = [
        FlaggedSentence(
            sentence=f"Sentence {i}.",
            cliches=[
                ClicheHit(canonical="test_phrase", variant="test_phrase", score=1.0)
            ],
        )
        for i in range(n_issues)
    ]
    return AuditReport(
        cliche_result=DetectionResult(
            flagged_sentences=flagged,
            unique_cliches=["test_phrase"],
            total_sentences=n_issues,
            flagged_count=n_issues,
        ),
        monotony_result=MonotonyResult(
            flagged_openers=[],
            all_openers={},
            total_sentences=n_issues,
            monotony_score=0.0,
        ),
        template_result=TemplateResult(
            flagged_templates=[],
            all_templates={},
            total_sentences=n_issues,
            unique_templates=0,
            repetition_score=0.0,
        ),
        not_but_result=[],
    )


def _patch_message(search: str, replace: str) -> dict:
    """Build a fake LLM message that calls editor_apply_patch."""
    return {
        "tool_calls": [
            {
                "id": "tc1",
                "type": "function",
                "function": {
                    "name": "editor_apply_patch",
                    "arguments": json.dumps(
                        {"patches": [{"search": search, "replace": replace}]}
                    ),
                },
            }
        ]
    }


def _make_client(messages_per_call: list[dict]) -> MagicMock:
    """Return a mock LLMClient whose complete() yields one done event per invocation."""
    call_idx = [0]

    async def _complete(*args, **kwargs):
        idx = call_idx[0]
        call_idx[0] += 1
        msg = (
            messages_per_call[idx]
            if idx < len(messages_per_call)
            else messages_per_call[-1]
        )
        yield {"type": "done", "message": msg}

    client = MagicMock()
    client.complete = MagicMock(side_effect=_complete)
    return client


# Draft chosen so each word can be patched individually across iterations.
DRAFT = "alpha bravo charlie delta."
SETTINGS = {"model_name": "test-model"}
ENABLED = {"editor_apply_patch": True}


async def _run(client, audit_side_effects):
    """Run editor_pass with mocked _run_contextual_audit and collect all events."""
    audit_iter = iter(audit_side_effects)
    with patch(
        "backend.passes.refine.refine._run_contextual_audit",
        side_effect=lambda *a, **kw: next(audit_iter),
    ):
        return [
            event
            async for event in editor_pass(
                client=client,
                prefix=[],
                effective_msg="Write something.",
                draft=DRAFT,
                settings=SETTINGS,
                phrase_bank=[],
                audit_enabled=True,
                enabled_tools=ENABLED,
                reasoning_on=False,
            )
        ]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestRefineLoopTermination:

    async def test_stops_early_when_all_issues_fixed(self):
        """Loop exits after one iteration when the post-patch audit is clean."""
        audit_side_effects = [
            (_dirty_report(2), "2 issues"),  # initial audit
            (AuditReport.clean(), "clean"),  # post-patch → loop breaks
        ]
        client = _make_client([_patch_message("alpha", "ALPHA")])

        events = await _run(client, audit_side_effects)

        done = next(e for e in events if e["type"] == "done")
        assert (
            client.complete.call_count == 1
        ), "Expected exactly 1 LLM call when audit clears after first patch"
        assert done["draft"] is not None, "Draft should be marked as changed"
        assert "ALPHA" in done["draft"], "Patch should have been applied to the draft"

    async def test_updated_audit_report_sent_each_iteration(self):
        """Each LLM call receives the audit report from the *previous* iteration's result."""
        # Unique report-text labels let us verify which report reached each LLM call.
        audit_side_effects = [
            (_dirty_report(3), "REPORT_LABEL_3"),  # initial
            (
                _dirty_report(2),
                "REPORT_LABEL_2",
            ),  # post-iter-0 → becomes tool-result for iter-1
            (
                _dirty_report(1),
                "REPORT_LABEL_1",
            ),  # post-iter-1 → becomes tool-result for iter-2
            (AuditReport.clean(), "clean"),  # post-iter-2 → loop breaks
        ]

        captured_msgs: list[list[dict]] = []
        call_idx = [0]
        patches_by_iter = [
            ("alpha", "ALPHA"),
            ("bravo", "BRAVO"),
            ("charlie", "CHARLIE"),
        ]

        async def _complete_capture(messages, **kwargs):
            captured_msgs.append(list(messages))
            idx = call_idx[0]
            call_idx[0] += 1
            s, r = patches_by_iter[idx]
            yield {"type": "done", "message": _patch_message(s, r)}

        client = MagicMock()
        client.complete = MagicMock(side_effect=_complete_capture)

        await _run(client, audit_side_effects)

        assert (
            client.complete.call_count == 3
        ), "Expected 3 iterations before audit cleared"

        # Iteration 0 — last user message is the initial refine prompt (contains REPORT_LABEL_3)
        assert "REPORT_LABEL_3" in captured_msgs[0][-1]["content"]

        # Iteration 1 — last user message is the tool-result turn (contains REPORT_LABEL_2)
        assert "REPORT_LABEL_2" in captured_msgs[1][-1]["content"]

        # Iteration 2 — last user message is the tool-result turn (contains REPORT_LABEL_1)
        assert "REPORT_LABEL_1" in captured_msgs[2][-1]["content"]

    async def test_hits_max_iterations_when_issues_persist(self):
        """Loop runs exactly MAX_EDITOR_ITERATIONS when issues decrease but never reach zero."""
        n = MAX_EDITOR_ITERATIONS
        # Audit results: initial count then one per iteration, always decreasing but never clean.
        audit_side_effects = [
            (_dirty_report(n + 1), f"{n + 1} issues"),  # initial
        ] + [
            (_dirty_report(n - i), f"{n - i} issues")  # post each iter
            for i in range(n)
        ]
        # Last post-iter audit has 1 issue (n - (n-1) = 1), so the loop exhausts all turns.

        patches_by_iter = [
            ("alpha", "ALPHA"),
            ("bravo", "BRAVO"),
            ("charlie", "CHARLIE"),
        ]
        call_idx = [0]

        async def _complete(*args, **kwargs):
            idx = call_idx[0]
            call_idx[0] += 1
            s, r = patches_by_iter[idx]
            yield {"type": "done", "message": _patch_message(s, r)}

        client = MagicMock()
        client.complete = MagicMock(side_effect=_complete)

        events = await _run(client, audit_side_effects)

        done = next(e for e in events if e["type"] == "done")
        assert (
            client.complete.call_count == MAX_EDITOR_ITERATIONS
        ), f"Expected exactly {MAX_EDITOR_ITERATIONS} LLM calls when the turn limit is reached"
        assert (
            done["draft"] is not None
        ), "Draft should be changed after patches were applied"
