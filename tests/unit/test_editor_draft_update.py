"""draft_update emission from the editor's ReAct loop, both transports.

Chat path: the draft mutates once per iteration → one draft_update per
iteration. Text-mode prefill path: one draft_update per forced per-finding
call (incremental previews), plus the batch-apply event whose text must be
byte-identical to the last preview.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.analysis import AuditReport
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import (
    ClicheHit,
    DetectionResult,
    FlaggedSentence,
)
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.inference import CachedBase, LLMClient, enabled_schemas
from backend.pipeline.passes.editor.editor import editor_pass
from backend.pipeline.passes.editor.feedback import FeedbackResult
from backend.pipeline.replay import CANONICAL_DRAFT_BLOCK, WriterReplay

SETTINGS = {
    "model_name": "test-model",
    "enable_agent": 1,
    "enabled_tools": {"editor_apply_patch": True},
    "reasoning_enabled_passes": {},
}


def _make_report(sentences: list[str]) -> AuditReport:
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


def _make_base() -> CachedBase:
    return CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})),
        model="test-model",
    )


async def _run(client: LLMClient, audits: list[AuditReport], draft: str) -> list[dict]:
    """Drive editor_pass with a scripted audit sequence; return yielded events."""
    audit_iter = iter(audits)

    async def fake_audit(draft, phrase_bank, prev_msgs, audit_toggles=None, user_message=""):
        try:
            return next(audit_iter), "audit text"
        except StopIteration:
            pytest.fail("unexpected extra audit call")

    events = []
    with patch("backend.pipeline.passes.editor.editor._run_contextual_audit", new=fake_audit):
        async for event in editor_pass(
            client,
            _make_base(),
            effective_msg="user msg",
            draft=draft,
            settings=SETTINGS,
            phrase_bank=[[]],
            audit_enabled=True,
            length_guard=None,
        ):
            events.append(event)
    return events


async def test_chat_path_emits_draft_update_per_iteration():
    client = LLMClient("http://localhost:9999")

    async def fake_complete(*args, **kwargs):
        yield {
            "type": "done",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "function": {
                            "name": "editor_apply_patch",
                            "arguments": json.dumps({"patches": [{"search": "Sentence 0.", "replace": "Fixed 0."}]}),
                        },
                    }
                ],
            },
        }

    client.complete = fake_complete

    # Initial audit: 2 issues (loop starts). Post-patch: clean (loop stops).
    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")

    assert [e["type"] for e in events] == ["draft_update", "done"]
    assert events[0]["draft"] == "Fixed 0. Sentence 1."
    assert events[1]["draft"] == "Fixed 0. Sentence 1."


async def test_non_reasoning_editor_keeps_a_writer_tool_replay_structured_across_iterations():
    client = LLMClient("http://localhost:9999")
    captured_messages: list[list[dict]] = []
    responses = [
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "edit-1",
                    "function": {
                        "name": "editor_apply_patch",
                        "arguments": json.dumps({"patches": [{"search": "Sentence 0.", "replace": "Fixed 0."}]}),
                    },
                }
            ],
        },
        {"content": "", "tool_calls": []},
    ]

    async def fake_complete(messages, **_kwargs):
        captured_messages.append(messages)
        yield {"type": "done", "message": responses.pop(0)}

    client.complete = fake_complete
    audits = iter(
        [
            _make_report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
            _make_report(["Sentence 1.", "Sentence 2."]),
        ]
    )

    async def fake_audit(_draft, _phrase_bank, _prev_msgs, audit_toggles=None, user_message=""):
        _ = audit_toggles, user_message
        return next(audits), "audit text"

    draft = "Sentence 0. Sentence 1. Sentence 2."
    replay = WriterReplay(
        messages=(
            {"role": "user", "content": "request"},
            {
                "role": "assistant",
                "content": "Sentence 0. ",
                "tool_calls": [{"id": "writer-1", "function": {"name": "orb_writer_a--b", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "writer-1", "content": '{"status":"ok"}'},
            {"role": "assistant", "content": "Sentence 1. Sentence 2."},
        ),
        canonical_draft_block=CANONICAL_DRAFT_BLOCK.format(draft=draft),
    )

    with patch("backend.pipeline.passes.editor.editor._run_contextual_audit", new=fake_audit):
        _ = [
            event
            async for event in editor_pass(
                client,
                _make_base(),
                effective_msg="request",
                draft=draft,
                settings=SETTINGS,
                phrase_bank=[[]],
                audit_enabled=True,
                reasoning_on=False,
                writer_user_msg="request",
                replay=replay,
            )
        ]

    assert len(captured_messages) == 2
    second = captured_messages[1]
    assert any(
        message.get("role") == "assistant"
        and any(call.get("function", {}).get("name") == "editor_apply_patch" for call in message.get("tool_calls", []))
        for message in second
    )
    assert not any(
        message.get("role") == "assistant" and message.get("content") == "Fixed 0. Sentence 1. Sentence 2."
        for message in second
    )


async def test_feedback_replay_names_the_post_editor_draft():
    client = LLMClient("http://localhost:9999")
    captured: dict = {}

    async def fake_edit_loop(*_args, **_kwargs):
        yield {"type": "done", "draft": "refined draft", "debug": "", "elapsed": 0, "tool_calls": []}

    async def fake_feedback(*_args, replay=None, **_kwargs):
        captured["replay"] = replay
        yield {"type": "done", "result": FeedbackResult()}

    replay = WriterReplay(
        messages=(
            {"role": "user", "content": "request"},
            {"role": "assistant", "content": "original ", "tool_calls": [{"id": "writer-1"}]},
            {"role": "tool", "tool_call_id": "writer-1", "content": "{}"},
            {"role": "assistant", "content": "draft"},
        ),
        canonical_draft_block=CANONICAL_DRAFT_BLOCK.format(draft="original draft"),
    )

    with (
        patch("backend.pipeline.passes.editor.editor._run_edit_loop", new=fake_edit_loop),
        patch("backend.pipeline.passes.editor.editor.feedback_step", new=fake_feedback),
    ):
        _ = [
            event
            async for event in editor_pass(
                client,
                _make_base(),
                effective_msg="request",
                draft="original draft",
                settings=SETTINGS,
                phrase_bank=[],
                writer_user_msg="request",
                replay=replay,
                feedback_fragments=[{"id": "feedback"}],
            )
        ]

    feedback_replay = captured["replay"]
    assert "refined draft" in feedback_replay.canonical_draft_block
    assert "original draft" not in feedback_replay.canonical_draft_block
    assert feedback_replay.messages == replay.messages


async def test_prefill_path_emits_draft_update_per_forced_call():
    client = LLMClient("http://localhost:9999", completion_mode="text")

    async def fake_complete(messages, model, tools=None, tool_choice=None, **params):
        # Text-transport forced-call contract: arguments = prefill + remainder.
        arguments = params["prefill"] + 'NEW"}]}'
        yield {
            "type": "done",
            "message": {
                "content": "",
                "tool_calls": [{"id": "tc1", "function": {"name": "editor_apply_patch", "arguments": arguments}}],
            },
        }

    client.complete = fake_complete

    # Both flagged sentences occur uniquely in the draft → two prefill targets.
    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")

    # Two per-call previews, then the batch apply (byte-identical to the last
    # preview), then done with the same authoritative text.
    assert [e["type"] for e in events] == ["draft_update", "draft_update", "draft_update", "done"]
    assert events[0]["draft"] == "NEW Sentence 1."
    assert events[1]["draft"] == "NEW NEW"
    assert events[2]["draft"] == events[1]["draft"]
    assert events[3]["draft"] == "NEW NEW"
