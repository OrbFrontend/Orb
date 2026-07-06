"""
Regression test: editor_pass must accept an explicit audit_context_msgs list
and use it instead of extracting previous assistant messages from prefix.

The bug this guards: during handle_super_regenerate, the prefix includes
target["content"] (the message being replaced) as an assistant message.  Without
the fix, the editor's repetition scanner picked up that message as "prior context"
and flagged the new draft for repeating the message it was literally told to replace.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.analysis import AuditReport
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import DetectionResult
from backend.analysis.detectors.structural_repetition import StructuralResult
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.inference import CachedBase, LLMClient, enabled_schemas
from backend.pipeline.passes.editor.editor import editor_pass


def _editor_base(prefix: list[dict]) -> CachedBase:
    """Build a CachedBase wrapping *prefix* with the patch tool enabled, as the
    super-regen path does. These tests only exercise audit-context derivation
    (which reads base.prefix), so the tool blob just needs to be non-empty."""
    return CachedBase(
        prefix=tuple(prefix),
        tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})),
        model="test-model",
    )


def _clean_report() -> AuditReport:
    return AuditReport(
        cliche_result=DetectionResult(flagged_sentences=[], unique_cliches=[], total_sentences=1, flagged_count=0),
        monotony_result=MonotonyResult([], {}, 0, 0.0),
        template_result=TemplateResult([], {}, 0, 0, 0.0),
        not_but_result=[],
        structural_repetition_result=None,
    )


def _repetitive_report() -> AuditReport:
    """Report that signals structural repetition."""
    return AuditReport(
        cliche_result=DetectionResult(flagged_sentences=[], unique_cliches=[], total_sentences=1, flagged_count=0),
        monotony_result=MonotonyResult([], {}, 0, 0.0),
        template_result=TemplateResult([], {}, 0, 0, 0.0),
        not_but_result=[],
        structural_repetition_result=StructuralResult(is_repetitive=True, min_similarity=0.9, mean_similarity=0.9, pairs=[]),
    )


@pytest.mark.asyncio
async def test_audit_context_msgs_overrides_prefix():
    """When audit_context_msgs is supplied, it must be used for the repetition
    scan instead of the assistant messages extracted from prefix."""
    client = LLMClient("http://localhost:9999")

    # Prefix contains the "old" assistant message (the one being superseded).
    replaced_msg = "She spun around. Her breath caught. The room fell silent."
    prefix = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "original user turn"},
        {"role": "assistant", "content": replaced_msg},
    ]

    captured_prev_msgs: list[list[str]] = []

    async def fake_contextual_audit(draft, phrase_bank, previous_assistant_msgs, audit_toggles=None, user_message=""):
        captured_prev_msgs.append(list(previous_assistant_msgs))
        return _clean_report(), ""

    with patch(
        "backend.pipeline.passes.editor.editor._run_contextual_audit",
        new=fake_contextual_audit,
    ):
        events = []
        async for event in editor_pass(
            client,
            _editor_base(prefix),
            effective_msg="user msg",
            draft="Some new draft text.",
            settings={"model_name": "test-model"},
            phrase_bank=[],
            audit_enabled=True,
            length_guard=None,
            audit_context_msgs=[],  # explicitly empty — no prior context
        ):
            events.append(event)

    assert len(captured_prev_msgs) == 1, "audit should run exactly once (clean result)"
    assert captured_prev_msgs[0] == [], (
        f"audit_context_msgs=[] must be forwarded directly; got {captured_prev_msgs[0]!r} instead (prefix-derived)"
    )


@pytest.mark.asyncio
async def test_no_audit_context_msgs_falls_back_to_prefix():
    """Without audit_context_msgs the existing behaviour is preserved: assistant
    messages are extracted from prefix in reverse order."""
    client = LLMClient("http://localhost:9999")

    prefix = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "first assistant"},
        {"role": "user", "content": "user turn"},
        {"role": "assistant", "content": "second assistant"},
    ]

    captured_prev_msgs: list[list[str]] = []

    async def fake_contextual_audit(draft, phrase_bank, previous_assistant_msgs, audit_toggles=None, user_message=""):
        captured_prev_msgs.append(list(previous_assistant_msgs))
        return _clean_report(), ""

    with patch(
        "backend.pipeline.passes.editor.editor._run_contextual_audit",
        new=fake_contextual_audit,
    ):
        async for _ in editor_pass(
            client,
            _editor_base(prefix),
            effective_msg="user msg",
            draft="Some draft.",
            settings={"model_name": "test-model"},
            phrase_bank=[],
            audit_enabled=True,
            length_guard=None,
            # audit_context_msgs omitted → derive from base.prefix
        ):
            pass

    assert len(captured_prev_msgs) == 1
    # Reversed prefix scan: most-recent assistant first
    assert captured_prev_msgs[0] == [
        "second assistant",
        "first assistant",
    ], f"Expected prefix-derived order but got {captured_prev_msgs[0]!r}"


@pytest.mark.asyncio
async def test_super_regen_prior_history_still_scanned():
    """The fix must be surgical: prior history messages (from before the turn
    being regenerated) are still passed to the scanner; only the replaced
    message itself is excluded."""
    client = LLMClient("http://localhost:9999")

    prior_msg = "He looked out the window. The city hummed below."
    replaced_msg = "She spun around. Her breath caught. The room fell silent."

    # prefix reflects extended_history: prior turn + the turn being replaced
    prefix = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": prior_msg},
        {"role": "user", "content": "original user"},
        {"role": "assistant", "content": replaced_msg},
    ]

    # audit_context_msgs mirrors what handle_super_regenerate computes from
    # history only: prior_msg is present, replaced_msg is not.
    audit_context_msgs = [prior_msg]

    captured_prev_msgs: list[list[str]] = []

    async def fake_contextual_audit(draft, phrase_bank, previous_assistant_msgs, audit_toggles=None, user_message=""):
        captured_prev_msgs.append(list(previous_assistant_msgs))
        return _clean_report(), ""

    with patch(
        "backend.pipeline.passes.editor.editor._run_contextual_audit",
        new=fake_contextual_audit,
    ):
        async for _ in editor_pass(
            client,
            _editor_base(prefix),
            effective_msg="[OOC: rewrite]",
            draft="Some new draft.",
            settings={"model_name": "test-model"},
            phrase_bank=[],
            audit_enabled=True,
            length_guard=None,
            audit_context_msgs=audit_context_msgs,
        ):
            pass

    assert len(captured_prev_msgs) == 1
    prev = captured_prev_msgs[0]
    assert prior_msg in prev, "prior history message must still be scanned"
    assert replaced_msg not in prev, "replaced message must be excluded from scan"


@pytest.mark.asyncio
async def test_super_regen_does_not_flag_replaced_message():
    """End-to-end shape of the super-regen fix: supplying audit_context_msgs that
    excludes the replaced message must result in a clean audit even when the
    replaced message would otherwise trigger structural repetition."""
    client = LLMClient("http://localhost:9999")

    replaced_msg = "She spun around. Her breath caught. The room fell silent."
    new_draft = "She spun around. Her breath caught. The room fell silent."  # identical

    prefix = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "original user"},
        {"role": "assistant", "content": replaced_msg},
    ]

    async def fake_contextual_audit(draft, phrase_bank, previous_assistant_msgs, audit_toggles=None, user_message=""):
        # Simulate the scanner flagging structural repetition when the replaced
        # message is present in the context, and clean otherwise.
        if replaced_msg in previous_assistant_msgs:
            return _repetitive_report(), "structural repetition detected"
        return _clean_report(), ""

    with patch(
        "backend.pipeline.passes.editor.editor._run_contextual_audit",
        new=fake_contextual_audit,
    ):
        events = []
        async for event in editor_pass(
            client,
            _editor_base(prefix),
            effective_msg="[OOC: rewrite]",
            draft=new_draft,
            settings={"model_name": "test-model"},
            phrase_bank=[],
            audit_enabled=True,
            length_guard=None,
            audit_context_msgs=[],  # super-regen passes history-only context
        ):
            events.append(event)

    done_events = [e for e in events if e.get("type") == "done"]
    assert len(done_events) == 1
    # Clean audit → no LLM call needed → draft is returned as None (unchanged)
    assert done_events[0]["draft"] is None, (
        "Editor should not attempt to rewrite when audit_context_msgs excludes the replaced message and the audit is clean"
    )
