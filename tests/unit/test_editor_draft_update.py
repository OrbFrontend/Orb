"""The editor's ReAct loop over id-anchored patches.

One draft mutation per iteration → one draft_update per iteration, on both
transports (the text-mode per-finding prefill path is gone; text endpoints
grammar-constrain the same single call from the tool schema instead).

Also pins the two things the id method made load-bearing: the ids the model
answers with are the ones from the report it was shown, and a re-audit
renumbers them with the change stated to the model.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.analysis import AuditReport, build_targets
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import (
    ClicheHit,
    DetectionResult,
    FlaggedSentence,
)
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.inference import (
    EDITOR_RENUMBER_NOTICE,
    CachedBase,
    LLMClient,
    enabled_schemas,
)
from backend.pipeline.passes.editor.editor import editor_pass

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


async def _run(client: LLMClient, audits: list[AuditReport], draft: str, **kwargs) -> list[dict]:
    """Drive editor_pass with a scripted audit sequence; return yielded events."""
    audit_iter = iter(audits)

    async def fake_audit(draft, phrase_bank, prev_msgs, audit_toggles=None, user_message=""):
        try:
            report = next(audit_iter)
        except StopIteration:
            pytest.fail("unexpected extra audit call")
        # Targets are rebuilt against the current draft, exactly as production does.
        return report, build_targets(report, draft)

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
            **kwargs,
        ):
            events.append(event)
    return events


def _patch_call(patches: list[dict]) -> dict:
    return {
        "type": "done",
        "message": {
            "content": "",
            "tool_calls": [
                {
                    "id": "tc1",
                    "function": {"name": "editor_apply_patch", "arguments": json.dumps({"patches": patches})},
                }
            ],
        },
    }


async def test_chat_path_emits_draft_update_per_iteration():
    client = LLMClient("http://localhost:9999")

    async def fake_complete(*args, **kwargs):
        yield _patch_call([{"id": 1, "replace": "Fixed 0."}])

    client.complete = fake_complete

    # Initial audit: 2 issues (loop starts). Post-patch: clean (loop stops).
    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")

    assert [e["type"] for e in events] == ["draft_update", "done"]
    assert events[0]["draft"] == "Fixed 0. Sentence 1."
    assert events[1]["draft"] == "Fixed 0. Sentence 1."


async def test_null_rewritten_text_stops_the_loop():
    # `"rewritten_text": null` is the model declining the rewrite. The default on
    # the .get() only covers an absent key, so a null used to reach .strip() and
    # abort the whole turn -- in a group beat, mid-beat. It must read as an empty
    # rewrite and stop the loop with the draft intact.
    client = LLMClient("http://localhost:9999")

    async def fake_complete(*args, **kwargs):
        yield {
            "type": "done",
            "message": {
                "content": "",
                "tool_calls": [
                    {"id": "tc1", "function": {"name": "editor_rewrite", "arguments": json.dumps({"rewritten_text": None})}}
                ],
            },
        }

    client.complete = fake_complete

    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."])], "Sentence 0. Sentence 1.")

    assert [e["type"] for e in events] == ["done"]  # no draft_update: nothing was applied
    assert events[-1]["draft"] is None  # draft unchanged


async def test_text_path_takes_the_same_single_call():
    # Text endpoints used to issue one prefilled call per finding; they now take
    # the same id-anchored call, grammar-constrained from the tool schema.
    client = LLMClient("http://localhost:9999", completion_mode="text")
    calls: list[dict] = []

    async def fake_complete(messages, model, tools=None, tool_choice=None, **params):
        calls.append({"tool_choice": tool_choice, "params": params})
        yield _patch_call([{"id": 1, "replace": "NEW"}, {"id": 2, "replace": "ALSO NEW"}])

    client.complete = fake_complete

    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")

    assert len(calls) == 1
    assert "prefill" not in calls[0]["params"]
    assert "grammar" not in calls[0]["params"]
    assert [e["type"] for e in events] == ["draft_update", "done"]
    assert events[-1]["draft"] == "NEW ALSO NEW"


async def test_ids_address_the_report_the_model_was_shown():
    """Every id patches its own sentence — the second id must not be resolved
    against the post-first-patch text."""
    client = LLMClient("http://localhost:9999")

    async def fake_complete(*args, **kwargs):
        yield _patch_call([{"id": 3, "replace": "C."}, {"id": 1, "replace": "A much longer replacement."}])

    client.complete = fake_complete

    draft = "Alpha one. Beta two. Gamma three."
    events = await _run(client, [_make_report(["Alpha one.", "Beta two.", "Gamma three."]), _make_report([])], draft)
    assert events[-1]["draft"] == "A much longer replacement. Beta two. C."


async def test_structured_replay_tells_the_model_the_ids_moved():
    """Reasoning models see their own previous call replayed beside a freshly
    numbered report, so the renumbering has to be stated, not inferred."""
    client = LLMClient("http://localhost:9999")
    sent: list[list[dict]] = []

    async def fake_complete(messages, model, tools=None, tool_choice=None, **params):
        sent.append([dict(m) for m in messages])
        yield _patch_call([{"id": 1, "replace": "Fixed 0."}])

    client.complete = fake_complete

    # 3 issues → 2 issues → clean: two LLM calls, so the second one carries the
    # replayed tool result.
    await _run(
        client,
        [
            _make_report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
            _make_report(["Sentence 1.", "Sentence 2."]),
            _make_report([]),
        ],
        "Sentence 0. Sentence 1. Sentence 2.",
        reasoning_on=True,
    )

    assert len(sent) == 2
    tool_msgs = [m for m in sent[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert EDITOR_RENUMBER_NOTICE in tool_msgs[0]["content"]
    assert "[1]" in tool_msgs[0]["content"]


async def test_apply_errors_reach_the_model_in_id_vocabulary():
    client = LLMClient("http://localhost:9999")
    sent: list[list[dict]] = []
    call = 0

    async def fake_complete(messages, model, tools=None, tool_choice=None, **params):
        nonlocal call
        sent.append([dict(m) for m in messages])
        call += 1
        # First call names an id the report never issued; second one is valid.
        yield _patch_call([{"id": 99, "replace": "X."}] if call == 1 else [{"id": 1, "replace": "Fixed 0."}])

    client.complete = fake_complete

    await _run(
        client,
        [
            _make_report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
            _make_report(["Sentence 1.", "Sentence 2."]),
            _make_report([]),
        ],
        "Sentence 0. Sentence 1. Sentence 2.",
        reasoning_on=True,
    )

    tool_msgs = [m for m in sent[1] if m.get("role") == "tool"]
    assert "no finding with id 99" in tool_msgs[0]["content"]
    assert "Valid ids: 1-3." in tool_msgs[0]["content"]
