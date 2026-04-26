"""
test_editor_loop.py — Tests for the ReAct loop in editor_pass.

Verifies that:
  1. The loop terminates early when all issues are fixed (audit clean).
  2. The updated audit report is sent back to the model each iteration.
  3. The loop runs exactly MAX_EDITOR_ITERATIONS when issues decrease but never clear.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from backend.passes.editor.audit import AuditReport
from backend.passes.editor.slop_detector import (
    DetectionResult,
    FlaggedSentence,
    ClicheHit,
)
from backend.passes.editor.opening_monotony import MonotonyResult
from backend.passes.editor.template_repetition import TemplateResult
from backend.passes.editor.structural_repetition import (
    StructuralResult,
    MessageStructure,
)
from backend.passes.editor.editor import editor_pass
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


def _structural_dirty_report() -> AuditReport:
    """Return an AuditReport flagged only for structural repetition."""
    return AuditReport(
        cliche_result=DetectionResult([], [], 0, 0),
        monotony_result=MonotonyResult([], {}, 0, 0.0),
        template_result=TemplateResult([], {}, 0, 0, 0.0),
        not_but_result=[],
        structural_repetition_result=StructuralResult(
            is_repetitive=True,
            min_similarity=0.9,
            mean_similarity=0.9,
            shared_skeleton=["narration", "dialogue"],
            messages=[
                MessageStructure(index=0, signature=["narration", "dialogue"]),
                MessageStructure(index=1, signature=["narration", "dialogue"]),
            ],
        ),
    )


def _rewrite_message(text: str) -> dict:
    """Build a fake LLM message that calls editor_rewrite."""
    return {
        "tool_calls": [
            {
                "id": "tc_rw",
                "type": "function",
                "function": {
                    "name": "editor_rewrite",
                    "arguments": json.dumps({"rewritten_text": text}),
                },
            }
        ]
    }


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
        "backend.passes.editor.editor._run_contextual_audit",
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


class TesteditorLoopTermination:

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

        # Iteration 0 — last user message is the initial editor prompt (contains REPORT_LABEL_3)
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


class TestEditorLoopToolSwitching:
    """Verify the loop dynamically switches tools and instructions based on post-action audits."""

    async def test_switches_to_apply_patch_after_rewrite_leaves_slop(self):
        """When editor_rewrite is forced by structural repetition but the rewritten
        text still has non-structural audit issues, the next iteration must use
        editor_apply_patch — both in the available tool list, the tool_choice, and
        the instruction text sent to the model."""

        # Draft that survives the rewrite and still contains a word we can patch.
        REWRITTEN_DRAFT = "alpha bravo charlie delta with a cliche."

        audit_side_effects = iter(
            [
                (
                    _structural_dirty_report(),
                    "structural issues",
                ),  # initial → forces rewrite
                (_dirty_report(1), "1 cliche issue"),  # post-rewrite → forces patch
                (AuditReport.clean(), "clean"),  # post-patch → done
            ]
        )

        responses = [
            _rewrite_message(REWRITTEN_DRAFT),
            _patch_message("cliche", "fresh word"),
        ]
        call_idx = [0]
        captured: list[dict] = []

        async def _complete_capture(**kwargs):
            captured.append(
                {
                    "tool_names": {t["function"]["name"] for t in kwargs["tools"]},
                    "tool_choice": kwargs["tool_choice"],
                    "last_user_content": kwargs["messages"][-1]["content"],
                }
            )
            idx = call_idx[0]
            call_idx[0] += 1
            yield {"type": "done", "message": responses[idx]}

        client = MagicMock()
        client.complete = MagicMock(side_effect=_complete_capture)

        with patch(
            "backend.passes.editor.editor._run_contextual_audit",
            side_effect=lambda *a, **kw: next(audit_side_effects),
        ):
            events = [
                event
                async for event in editor_pass(
                    client=client,
                    prefix=[],
                    effective_msg="Write something.",
                    draft=DRAFT,
                    settings=SETTINGS,
                    phrase_bank=[],
                    audit_enabled=True,
                    enabled_tools={"editor_apply_patch": True, "editor_rewrite": True},
                    reasoning_on=False,
                )
            ]

        assert len(captured) == 2, "Expected exactly 2 LLM calls"

        # ── Iteration 0: must force editor_rewrite ────────────────────────────
        iter0 = captured[0]
        assert iter0["tool_choice"] == {
            "type": "function",
            "function": {"name": "editor_rewrite"},
        }, "First call must force editor_rewrite via tool_choice"
        assert (
            "editor_rewrite" in iter0["tool_names"]
        ), "editor_rewrite must be available in iteration 0"

        # ── Iteration 1: must switch to editor_apply_patch ────────────────────
        iter1 = captured[1]
        assert iter1["tool_choice"] == {
            "type": "function",
            "function": {"name": "editor_apply_patch"},
        }, "Second call must force editor_apply_patch via tool_choice"
        assert (
            "editor_apply_patch" in iter1["tool_names"]
        ), "editor_apply_patch must be available in iteration 1"
        assert (
            "editor_rewrite" not in iter1["tool_names"]
        ), "editor_rewrite must NOT be available in iteration 1"

        # ── Instruction text also switched ────────────────────────────────────
        # reasoning_on=False uses the old mutation path, so msgs[-1] is a rebuilt
        # editor prompt with updated instructions for the next tool.
        prompt1 = iter1["last_user_content"]
        assert (
            "editor_apply_patch" in prompt1
        ), "Iteration 1 prompt must reference editor_apply_patch"
        assert (
            "editor_rewrite" not in prompt1
        ), "Iteration 1 prompt must not reference editor_rewrite"

        # ── Final draft reflects both transformations ─────────────────────────
        done = next(e for e in events if e["type"] == "done")
        assert done["draft"] is not None, "Draft must be marked as changed"
        assert (
            "fresh word" in done["draft"]
        ), "Patch must have been applied to the rewritten draft"
