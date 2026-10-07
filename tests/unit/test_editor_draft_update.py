"""The editor's ReAct loop over id-anchored patches.

One draft mutation per iteration -> one draft_update per iteration, on both transports (the text-mode per-finding prefill path is
gone; text endpoints grammar-constrain the same single call from the tool schema instead).

Also pins the two things the id method made load-bearing: the ids the model answers with are the ones from the report it was
shown, and a re-audit renumbers them with the change stated to the model.
"""

import json
from collections.abc import Sequence
from unittest.mock import patch

import pytest

from backend.analysis import AuditReport, build_targets
from backend.analysis.detectors.opening_monotony import MonotonyResult
from backend.analysis.detectors.slop_detector import ClicheHit, DetectionResult, FlaggedSentence
from backend.analysis.detectors.structural_repetition import StructuralResult
from backend.analysis.detectors.template_repetition import TemplateResult
from backend.analysis.patching import apply_id_patches
from backend.inference import CachedBase, LLMClient
from backend.pipeline.passes.editor.editor import editor_pass
from backend.pipeline.passes.editor.length_guard import LengthGuard
from backend.pipeline.passes.editor.prompts import EDITOR_RENUMBER_NOTICE, PATCH_CATEGORY_RULES, patch_instructions
from backend.prompting.tool_catalog import enabled_schemas

SETTINGS = {
    "model_name": "test-model",
    "enable_agent": 1,
    "enabled_tools": {"editor_apply_patch": True},
    "reasoning_enabled_passes": {},
}


def _make_report(sentences: list[str], not_but: Sequence[str] = ()) -> AuditReport:
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
        not_but_result=[{"sentence": sentence} for sentence in not_but],
        structural_repetition_result=None,
    )


def _make_base(tools: Sequence[str] = ("editor_apply_patch",)) -> CachedBase:
    return CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas(dict.fromkeys(tools, True), {})),
        model="test-model",
    )


async def _run(
    client: LLMClient,
    audits: list[AuditReport],
    draft: str,
    *,
    tools: Sequence[str] = ("editor_apply_patch",),
    length_guard: LengthGuard | None = None,
    **kwargs,
) -> list[dict]:
    """Run editor_pass with a scripted audit sequence and strip its step marker."""
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
            _make_base(tools),
            effective_msg="user msg",
            draft=draft,
            settings=SETTINGS,
            phrase_bank=[[]],
            audit_enabled=True,
            length_guard=length_guard,
            **kwargs,
        ):
            events.append(event)
    assert events[0] == {"type": "step", "step": "output_auditor"}
    return events[1:]


def _call(name: str, arguments: dict) -> dict:
    return {
        "type": "done",
        "message": {
            "content": "",
            "tool_calls": [{"id": "tc1", "function": {"name": name, "arguments": json.dumps(arguments)}}],
        },
    }


def _patch_call(patches: list[dict]) -> dict:
    return _call("editor_apply_patch", {"patches": patches})


def _client(*responses: dict, seen: list | None = None, mode: str = "chat") -> LLMClient:
    """A client whose `complete` answers *responses* in order (repeating the last), recording each request into *seen*."""
    client = LLMClient("http://localhost:9999", completion_mode=mode)
    calls: list[dict] = [] if seen is None else seen

    async def fake_complete(messages, model, tools=None, tool_choice=None, **params):
        calls.append({"messages": [dict(m) for m in messages], "tool_choice": tool_choice, "params": params})
        yield responses[min(len(calls), len(responses)) - 1]

    client.complete = fake_complete  # type: ignore[method-assign]
    return client


def _tool_turns(call: dict) -> list[str]:
    return [m["content"] for m in call["messages"] if m.get("role") == "tool"]


THREE = "Sentence 0. Sentence 1. Sentence 2."


async def test_chat_path_emits_draft_update_per_iteration():
    # Initial audit: 2 issues (loop starts). Post-patch: clean (loop stops).
    client = _client(_patch_call([{"id": 1, "replace": "Fixed 0."}]))
    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")
    assert [(e["type"], e["draft"]) for e in events] == [
        ("draft_update", "Fixed 0. Sentence 1."),
        ("done", "Fixed 0. Sentence 1."),
    ]


async def test_null_rewritten_text_stops_the_loop():
    # `"rewritten_text": null` is the model declining the rewrite; it must read as an empty rewrite, not reach .strip() and
    # abort the turn, and stop the loop with the draft intact.
    client = _client(_call("editor_rewrite", {"rewritten_text": None}))
    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."])], "Sentence 0. Sentence 1.")
    assert [(e["type"], e["draft"]) for e in events] == [("done", None)]  # nothing applied


async def test_findings_with_no_target_end_the_pass_without_a_call():
    # Neither flagged sentence is in the draft, so neither gets an id; with nothing addressable, even editor_rewrite on offer
    # is no reason to send a call.
    seen: list = []
    events = await _run(
        _client(seen=seen),
        [_make_report(["Not in the draft.", "Nor is this."])],
        "Sentence 0. Sentence 1.",
        tools=("editor_apply_patch", "editor_rewrite"),
    )
    assert seen == []
    assert [(e["type"], e["draft"]) for e in events] == [("done", None)]


_IDLE_GUARD: LengthGuard = {"enforce": False, "max_words": 10_000, "max_paragraphs": 100}


@pytest.mark.parametrize(
    ("tools", "length_guard", "forced"),
    [
        (("editor_apply_patch",), _IDLE_GUARD, "editor_apply_patch"),
        (("editor_apply_patch", "editor_rewrite"), None, "editor_apply_patch"),
        (("editor_apply_patch", "editor_rewrite"), _IDLE_GUARD, "editor_rewrite"),
    ],
)
async def test_structural_repetition_rewrites_only_with_the_length_guard_on_and_the_tool_offered(tools, length_guard, forced):
    # The blob offers editor_rewrite whenever the Agent is on; the length guard opts a turn into whole-draft rewrites. Forcing a
    # tool the request does not carry gets prose back, never a call, so without it the patchable findings get patched.
    seen: list = []
    report = _make_report(["Sentence 0.", "Sentence 1."])
    report.structural_repetition_result = StructuralResult(
        is_repetitive=True, min_similarity=0.9, mean_similarity=0.9, shared_skeleton=None, messages=[]
    )
    await _run(
        _client({"type": "done", "message": {"content": "", "tool_calls": []}}, seen=seen),
        [report],
        "Sentence 0. Sentence 1.",
        tools=tools,
        length_guard=length_guard,
    )
    assert [call["tool_choice"]["function"]["name"] for call in seen] == [forced]


async def test_text_path_takes_the_same_single_call():
    # Text endpoints take the same id-anchored call as chat, grammar-constrained from the tool schema, not prefilled per finding.
    seen: list = []
    client = _client(_patch_call([{"id": 1, "replace": "NEW"}, {"id": 2, "replace": "ALSO NEW"}]), seen=seen, mode="text")
    events = await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")
    [call] = seen
    assert "prefill" not in call["params"] and "grammar" not in call["params"]
    assert [e["type"] for e in events] == ["draft_update", "done"]
    assert events[-1]["draft"] == "NEW ALSO NEW"


async def test_ids_address_the_report_the_model_was_shown():
    """Every id patches its own sentence -- the second id must not be resolved against the post-first-patch text."""
    client = _client(_patch_call([{"id": 3, "replace": "C."}, {"id": 1, "replace": "A much longer replacement."}]))
    draft = "Alpha one. Beta two. Gamma three."
    events = await _run(client, [_make_report(["Alpha one.", "Beta two.", "Gamma three."]), _make_report([])], draft)
    assert events[-1]["draft"] == "A much longer replacement. Beta two. C."


async def test_structured_replay_tells_the_model_the_ids_moved():
    """Reasoning models see their own previous call replayed beside a freshly numbered report, so the renumbering is stated."""
    seen: list = []
    reports = [
        _make_report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
        _make_report(["Sentence 1.", "Sentence 2."]),
        _make_report([]),
    ]
    await _run(_client(_patch_call([{"id": 1, "replace": "Fixed 0."}]), seen=seen), reports, THREE, reasoning_on=True)
    assert len(seen) == 2  # the second call carries the replayed tool result
    [tool_msg] = _tool_turns(seen[1])
    assert EDITOR_RENUMBER_NOTICE in tool_msg
    assert "[1]" in tool_msg


async def test_request_carries_only_the_flagged_kinds_rules():
    seen: list = []
    client = _client(_patch_call([{"id": 1, "replace": "Fixed 0."}]), seen=seen)
    await _run(client, [_make_report(["Sentence 0.", "Sentence 1."]), _make_report([])], "Sentence 0. Sentence 1.")
    request = seen[0]["messages"][-1]["content"]
    assert "PATCHING RULES:" in request
    assert PATCH_CATEGORY_RULES["banned_phrases"] in request
    assert not any(rule in request for kind, rule in PATCH_CATEGORY_RULES.items() if kind != "banned_phrases")


def test_patch_instructions_owe_the_whole_block_until_one_was_sent():
    rule = "- " + PATCH_CATEGORY_RULES["anti_echo"]
    # After a rewrite request nothing about patching has been sent yet.
    assert patch_instructions({"anti_echo"}).startswith("Use `editor_apply_patch`")
    assert patch_instructions({"anti_echo"}).endswith(rule)
    assert patch_instructions({"anti_echo"}, shown=set()) == rule
    assert patch_instructions({"anti_echo"}, shown={"anti_echo"}) == ""


async def test_structured_replay_adds_rules_for_newly_flagged_kinds():
    """A kind first flagged in a later report brings its rule along; rules already sent are not repeated."""
    seen: list = []
    reports = [
        _make_report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
        _make_report(["Sentence 1."], not_but=["Sentence 2."]),
        _make_report([]),
    ]
    await _run(_client(_patch_call([{"id": 1, "replace": "Fixed 0."}]), seen=seen), reports, THREE, reasoning_on=True)
    assert len(seen) == 2
    tool_msg = _tool_turns(seen[1])[0]
    assert PATCH_CATEGORY_RULES["contrastive_negation"] in tool_msg
    assert PATCH_CATEGORY_RULES["banned_phrases"] not in tool_msg
    assert "PATCHING RULES:" not in tool_msg


async def test_apply_errors_reach_the_model_in_id_vocabulary():
    # The first call names an id the report never issued; the second one is valid.
    seen: list = []
    client = _client(_patch_call([{"id": 99, "replace": "X."}]), _patch_call([{"id": 1, "replace": "Fixed 0."}]), seen=seen)
    reports = [
        _make_report(["Sentence 0.", "Sentence 1.", "Sentence 2."]),
        _make_report(["Sentence 1.", "Sentence 2."]),
        _make_report([]),
    ]
    await _run(client, reports, THREE, reasoning_on=True)
    tool_msg = _tool_turns(seen[1])[0]
    assert "no finding with id 99" in tool_msg
    assert "Valid ids: 1-3." in tool_msg


# -- The protected-sequence guard, in the loop ---------------------------------
#
# Two audit findings, so a patch rejected on one still leaves the loop a target -- a single-target fixture measures the patch
# function, not the orchestration around it. What these pin is the guard's real user-visible effect: a rejected patch means the
# flagged span *keeps its slop*, not that it gets a better repair.

GUARDED_DRAFT = (
    '"Don\'t touch it," Mara said. She said softly, her voice thick with tension. '
    '"I wasn\'t going to," Ilya replied. The silence was deafening.'
)
GUARDED_NARRATION = "She said softly, her voice thick with tension."
GUARDED_CLOSER = "The silence was deafening."


async def test_every_patch_rejected_stops_the_loop_with_the_draft_intact():
    # Both replacements copy protected dialogue, so nothing applies, the issue count cannot move, and the no-progress stop
    # fires. One bad patch per target abandons both repairs -- defensible (intact writer text beats a corrupt splice) but worth
    # seeing asserted before any retry policy lands.
    client = _client(
        _patch_call(
            [
                {"id": 1, "replace": "Don't touch it, she whispered again."},
                {"id": 2, "replace": "I wasn't going to, he said again."},
            ]
        )
    )
    events = await _run(client, [_make_report([GUARDED_NARRATION, GUARDED_CLOSER])] * 2, GUARDED_DRAFT)

    assert [e["type"] for e in events] == ["draft_update", "done"]
    assert events[0]["draft"] == GUARDED_DRAFT  # the iteration changed nothing
    assert events[-1]["draft"] is None  # and the pass reports the draft unchanged


async def test_a_rejected_patch_does_not_block_its_neighbour():
    # One clone, one clean replacement: the clean one lands, the flagged span behind the rejection keeps the writer's original
    # text, and when the retry clones again the no-progress stop ends the pass with it still unrepaired.
    client = _client(
        _patch_call([{"id": 1, "replace": "Don't touch it, she whispered again."}, {"id": 2, "replace": "Nobody spoke."}])
    )
    audits = [_make_report([GUARDED_NARRATION, GUARDED_CLOSER]), *[_make_report([GUARDED_NARRATION])] * 2]
    events = await _run(client, audits, GUARDED_DRAFT)

    assert events[-1]["draft"] == GUARDED_DRAFT.replace(GUARDED_CLOSER, "Nobody spoke.")
    assert GUARDED_NARRATION in events[-1]["draft"]


async def test_thinking_mode_is_told_when_and_why_a_patch_was_rejected():
    # Without this the model is left believing its patch landed: the rejected target keeps the writer's text, and its retry
    # repeats the rejected clone unless the tool-result turn carries the reason.
    seen: list = []
    client = _client(
        _patch_call([{"id": 1, "replace": "Don't touch it, she whispered again."}, {"id": 2, "replace": "Nobody spoke."}]),
        _patch_call([{"id": 1, "replace": "Her hand fell away from the latch."}]),
        seen=seen,
    )

    audits = [_make_report([GUARDED_NARRATION, GUARDED_CLOSER]), _make_report([GUARDED_NARRATION]), _make_report([])]
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
    seen: list = []
    client = _client(_patch_call([{"id": 1, "replace": "Don't touch it, she whispered again."}]), seen=seen)
    events = await _run(client, [_make_report([GUARDED_NARRATION, GUARDED_CLOSER])] * 3, GUARDED_DRAFT, reasoning_on=True)

    assert len(seen) == 2  # one explanation, then the stop
    assert "copies protected text" in _tool_turns(seen[1])[0]
    assert events[-1]["draft"] is None  # nothing applied, writer's text intact


async def test_non_thinking_mode_still_stops_quietly():
    # The flat recap has no tool-result slot to carry the reason, so a pass whose only patch was rejected makes no progress and
    # stops: the flagged span keeps its slop, and the rejection is logged rather than replayed.
    seen: list = []
    client = _client(_patch_call([{"id": 1, "replace": "Don't touch it, she whispered again."}]), seen=seen)
    events = await _run(client, [_make_report([GUARDED_NARRATION, GUARDED_CLOSER])] * 2, GUARDED_DRAFT)

    assert len(seen) == 1
    assert events[-1]["draft"] is None


async def test_patching_an_ellipsis_beat_does_not_strand_its_continuation():
    # The reported failure: `a bit... more still than usual` is ONE sentence, and splitting at the ellipsis made its first half
    # an addressable target. The model's replacement ends in a full stop, so patching the fragment left `. more still than
    # usual.` -- a lowercase orphan -- in the saved reply.
    draft = "Heidi doesn't flinch. Her expression still open, although her emerald eyes seem a bit... more still than usual."
    beat = "Her expression still open, although her emerald eyes seem a bit... more still than usual."

    targets = build_targets(_make_report([beat]), draft)

    assert [t.span for t in targets] == [beat]  # the whole beat, not the half before the ellipsis

    patched, errors = apply_id_patches(draft, targets, [{"id": 1, "replace": "Her face stays open and inviting."}])
    assert not errors
    assert patched == "Heidi doesn't flinch. Her face stays open and inviting."
