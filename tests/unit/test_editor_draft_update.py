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
from backend.analysis.patching import apply_id_patches
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
    # abort the whole turn -- in a group exchange, mid-exchange. It must read as an empty
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


# ── The protected-sequence guard, in the loop ─────────────────────────────────
#
# Two audit findings, so the pass does not take the `total_issues <= 1` skip and
# the ReAct loop actually runs — a single-target fixture measures the patch
# function, not the orchestration around it. What these pin is the guard's real
# user-visible effect: a rejected patch means the flagged span *keeps its slop*,
# not that it gets a better repair.

GUARDED_DRAFT = (
    '"Don\'t touch it," Mara said. She said softly, her voice thick with tension. '
    '"I wasn\'t going to," Ilya replied. The silence was deafening.'
)
GUARDED_NARRATION = "She said softly, her voice thick with tension."
GUARDED_CLOSER = "The silence was deafening."


async def test_every_patch_rejected_stops_the_loop_with_the_draft_intact():
    # Both replacements copy protected dialogue, so nothing applies, the issue
    # count cannot move, and the no-progress stop fires. One bad patch per
    # target abandons both repairs — defensible (intact writer text beats a
    # corrupt splice) but worth seeing asserted before any retry policy lands.
    client = LLMClient("http://localhost:9999")

    async def fake_complete(*args, **kwargs):
        yield _patch_call(
            [
                {"id": 1, "replace": "Don't touch it, she whispered again."},
                {"id": 2, "replace": "I wasn't going to, he said again."},
            ]
        )

    client.complete = fake_complete

    audits = [_make_report([GUARDED_NARRATION, GUARDED_CLOSER])] * 2
    events = await _run(client, audits, GUARDED_DRAFT)

    assert [e["type"] for e in events] == ["draft_update", "done"]
    assert events[0]["draft"] == GUARDED_DRAFT  # the iteration changed nothing
    assert events[-1]["draft"] is None  # and the pass reports the draft unchanged


async def test_a_rejected_patch_does_not_block_its_neighbour():
    # One clone, one clean replacement: the clean one lands, the flagged span
    # behind the rejection keeps the writer's original text, and the `<= 1`
    # break ends the pass with it still unrepaired.
    client = LLMClient("http://localhost:9999")

    async def fake_complete(*args, **kwargs):
        yield _patch_call(
            [
                {"id": 1, "replace": "Don't touch it, she whispered again."},
                {"id": 2, "replace": "Nobody spoke."},
            ]
        )

    client.complete = fake_complete

    audits = [_make_report([GUARDED_NARRATION, GUARDED_CLOSER]), _make_report([GUARDED_NARRATION])]
    events = await _run(client, audits, GUARDED_DRAFT)

    assert events[-1]["draft"] == GUARDED_DRAFT.replace(GUARDED_CLOSER, "Nobody spoke.")
    assert GUARDED_NARRATION in events[-1]["draft"]


def _scripted(responses: list[dict], seen: list[list]):
    """A fake `complete` that answers with *responses* in order, recording messages."""

    async def fake_complete(messages, model, tools=None, tool_choice=None, **params):
        seen.append(list(messages))
        yield responses[min(len(seen) - 1, len(responses) - 1)]

    return fake_complete


def _tool_turns(messages: list) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "tool"]


async def test_thinking_mode_is_told_when_and_why_a_patch_was_rejected():
    # Without this the model is left believing its patch landed: the rejected
    # target keeps the writer's text, so the issue count cannot improve, so the
    # `<= 1` stop fires before the tool-result turn that carries the reason.
    client = LLMClient("http://localhost:9999")
    seen: list[list] = []
    client.complete = _scripted(
        [
            _patch_call(
                [
                    {"id": 1, "replace": "Don't touch it, she whispered again."},
                    {"id": 2, "replace": "Nobody spoke."},
                ]
            ),
            _patch_call([{"id": 1, "replace": "Her hand fell away from the latch."}]),
        ],
        seen,
    )

    audits = [
        _make_report([GUARDED_NARRATION, GUARDED_CLOSER]),
        _make_report([GUARDED_NARRATION]),
        _make_report([]),
    ]
    events = await _run(client, audits, GUARDED_DRAFT, reasoning_on=True)

    assert len(seen) == 2  # the loop did not stop on the rejection
    rejection = _tool_turns(seen[1])[0]
    assert "the patch for id 1 copies protected text from before the flagged span" in rejection
    assert "Don't touch it" in rejection  # *why*, in the draft's own words
    # And the second attempt lands, so the span the rejection saved is repaired.
    assert events[-1]["draft"] == GUARDED_DRAFT.replace(GUARDED_NARRATION, "Her hand fell away from the latch.").replace(
        GUARDED_CLOSER, "Nobody spoke."
    )


async def test_the_rejection_is_explained_once_not_chased_forever():
    # The extra iteration buys the model one informed attempt, not a retry loop:
    # a model that copies again gets the ordinary no-progress stop.
    client = LLMClient("http://localhost:9999")
    seen: list[list] = []
    client.complete = _scripted([_patch_call([{"id": 1, "replace": "Don't touch it, she whispered again."}])], seen)

    audits = [_make_report([GUARDED_NARRATION, GUARDED_CLOSER])] * 3
    events = await _run(client, audits, GUARDED_DRAFT, reasoning_on=True)

    assert len(seen) == 2  # one explanation, then the stop
    assert "copies protected text" in _tool_turns(seen[1])[0]
    assert events[-1]["draft"] is None  # nothing applied, writer's text intact


async def test_non_thinking_mode_still_stops_quietly():
    # The flat recap has no tool-result slot to carry the reason, so those models
    # keep the phase-one behaviour: the flagged span keeps its slop, and the
    # rejection is logged rather than replayed.
    client = LLMClient("http://localhost:9999")
    seen: list[list] = []
    client.complete = _scripted(
        [
            _patch_call(
                [
                    {"id": 1, "replace": "Don't touch it, she whispered again."},
                    {"id": 2, "replace": "Nobody spoke."},
                ]
            )
        ],
        seen,
    )

    audits = [_make_report([GUARDED_NARRATION, GUARDED_CLOSER]), _make_report([GUARDED_NARRATION])]
    events = await _run(client, audits, GUARDED_DRAFT)

    assert len(seen) == 1
    assert events[-1]["draft"] == GUARDED_DRAFT.replace(GUARDED_CLOSER, "Nobody spoke.")


async def test_the_rewriter_output_is_what_gets_audited():
    # The local prose rewriter runs BEFORE the audit, so every span the editor is
    # handed has to come from the rewritten prose. Auditing the writer's text and
    # patching the rewriter's would address offsets into a draft that no longer
    # exists. The ordering lives in editor_pass and had no test.
    writer = "Monika doesn't flinch. Her eyes seem a bit... more still than usual."
    rewritten = "Monika doesn't flinch. Her stare seems a bit... more fixed than usual."
    audited: list[str] = []

    async def fake_rewrite_events(draft, cfg):
        assert draft == writer  # the rewriter is the one pass that sees the writer's text
        yield {"type": "draft_update", "draft": rewritten}
        yield {"type": "rewritten", "draft": rewritten}

    async def spy_audit(draft, phrase_bank, prev_msgs, audit_toggles=None, user_message=""):
        audited.append(draft)
        report = _make_report([])
        return report, build_targets(report, draft)

    client = LLMClient("http://localhost:9999")
    seen: list[list] = []
    client.complete = _scripted([], seen)

    with (
        patch("backend.pipeline.passes.editor.editor.rewrite_events", new=fake_rewrite_events),
        patch("backend.pipeline.passes.editor.editor._run_contextual_audit", new=spy_audit),
    ):
        events = [
            e
            async for e in editor_pass(
                client,
                _make_base(),
                effective_msg="user msg",
                draft=writer,
                settings=SETTINGS,
                phrase_bank=[],
                audit_enabled=True,
                length_guard=None,
                prose_rewrite={"variant_id": "test", "gpu": True, "batch_size": 2},
            )
        ]

    assert audited == [rewritten]
    # A clean audit exits before the LLM loop, but the rewrite still has to be
    # announced as the absolute draft rather than the "unchanged" None.
    assert not seen
    assert events[-1]["draft"] == rewritten


async def test_patching_an_ellipsis_beat_does_not_strand_its_continuation():
    # The reported failure: `a bit... more still than usual` is ONE sentence, and
    # splitting at the ellipsis made its first half an addressable target. The
    # model's replacement ends in a full stop, so patching the fragment left
    # `. more still than usual.` -- a lowercase orphan -- in the saved reply.
    draft = "Monika doesn't flinch. Her expression still open, although her emerald eyes seem a bit... more still than usual."
    beat = "Her expression still open, although her emerald eyes seem a bit... more still than usual."

    report = _make_report([beat])
    targets = build_targets(report, draft)

    assert [t.span for t in targets] == [beat]  # the whole beat, not the half before the ellipsis

    patched, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "Her face stays open and inviting."}])
    assert not errors
    assert patched == "Monika doesn't flinch. Her face stays open and inviting."
