"""Unit tests for the editor's text-mode prefill patch path.

Target extraction and the prefill string are pure; the collector is exercised
against a fake client that plays back the transport contract (arguments =
prefill + generated remainder).
"""

from __future__ import annotations

import json

from backend.analysis import AuditReport, DetectionResult, FlaggedOpener, MonotonyResult
from backend.analysis.detectors.anti_echo import EchoResult, FlaggedEcho
from backend.analysis.detectors.phrase_repetition import FlaggedPhrase, PhraseResult
from backend.analysis.detectors.slop_detector import ClicheHit, FlaggedSentence
from backend.inference import TOOLS, CachedBase
from backend.pipeline.passes.editor.editor import (
    _PATCH_REMAINDER_GRAMMAR,
    MAX_PREFILL_TARGETS,
    _collect_prefill_patches,
    _patch_prefill,
    _prefill_targets,
)

# ── _patch_prefill ────────────────────────────────────────────────────────────


def test_patch_prefill_roundtrips_awkward_spans():
    for span in ['He said "hi" — twice.', "line\nbreak", "backslash \\ tab\t", "*emphasis* “smart”"]:
        full = _patch_prefill(span) + 'NEW"}]}'
        assert json.loads(full) == {"patches": [{"search": span, "replace": "NEW"}]}


# ── _prefill_targets ──────────────────────────────────────────────────────────


def _clean_report() -> AuditReport:
    return AuditReport.clean()


def test_prefill_targets_extracts_dedupes_and_filters_non_unique():
    draft = "Alpha one. Beta two. Beta three. Dup. Dup."
    r = _clean_report()
    r.cliche_result = DetectionResult(
        flagged_sentences=[
            FlaggedSentence("Alpha one.", [ClicheHit("alpha", 0.9)]),
            FlaggedSentence("Dup.", [ClicheHit("dup", 0.9)]),  # occurs twice → excluded
        ],
        unique_cliches=["alpha", "dup"],
        total_sentences=5,
        flagged_count=2,
    )
    # sentences[0] stays as the anchor; only the rest become targets.
    r.monotony_result = MonotonyResult(
        flagged_openers=[FlaggedOpener("Beta", 2, 2, 0.4, ["Beta two.", "Beta three."])],
        all_openers={},
        total_sentences=5,
        monotony_score=0.4,
    )
    r.not_but_result = [{"sentence": "Alpha one.", "is_parallel": False}]  # duplicate span → deduped
    r.phrase_result = PhraseResult(
        flagged_phrases=[FlaggedPhrase("beta two", 3, [0, 1], ["From an old message.", "Beta two."])],
        total_messages=4,
    )
    r.echo_result = EchoResult(flagged_echoes=[FlaggedEcho("Beta three.", "beta three", 2)])  # dup → deduped

    targets = _prefill_targets(r, draft)
    spans = [s for s, _ in targets]
    # Forward document order, not audit-category order (opener "Beta three." is
    # found before phrase "Beta two." in category order, but sits after it in the draft).
    assert spans == ["Alpha one.", "Beta two.", "Beta three."]
    whys = dict(targets)
    assert '"alpha"' in whys["Alpha one."]
    assert '"Beta"' in whys["Beta three."]
    assert '"beta two"' in whys["Beta two."]


def test_prefill_targets_capped():
    sentences = [f"Unique sentence number {i}." for i in range(MAX_PREFILL_TARGETS + 4)]
    draft = " ".join(sentences)
    r = _clean_report()
    r.cliche_result = DetectionResult(
        flagged_sentences=[FlaggedSentence(s, [ClicheHit("x", 1.0)]) for s in sentences],
        unique_cliches=["x"],
        total_sentences=len(sentences),
        flagged_count=len(sentences),
    )
    assert len(_prefill_targets(r, draft)) == MAX_PREFILL_TARGETS


# ── _collect_prefill_patches ──────────────────────────────────────────────────


class _FakeClient:
    """Plays back the text-transport forced-call contract."""

    is_aborted = False

    def __init__(self):
        self.calls: list[dict] = []

    async def complete(self, messages, model, tools=None, tool_choice=None, **params):
        self.calls.append({"messages": list(messages), "tool_choice": tool_choice, "params": params})
        arguments = params["prefill"] + 'NEW"}]}'
        yield {
            "type": "done",
            "message": {
                "content": "",
                "tool_calls": [
                    {"id": "call_0", "type": "function", "function": {"name": "editor_apply_patch", "arguments": arguments}}
                ],
            },
            "usage": None,
        }


async def test_collect_prefill_patches_one_forced_call_per_target():
    client = _FakeClient()
    base = CachedBase(prefix=({"role": "system", "content": "sys"},), tools=(TOOLS["editor_apply_patch"]["schema"],), model="m")
    draft = "First flagged. Second flagged."
    targets = [("First flagged.", "why one"), ("Second flagged.", "why two")]

    patches, debug = await _collect_prefill_patches(
        client, base, {"role": "user", "content": "user msg"}, draft, targets, {"temperature": 0.25}, None
    )

    assert patches == [
        {"search": "First flagged.", "replace": "NEW"},
        {"search": "Second flagged.", "replace": "NEW"},
    ]
    assert len(client.calls) == 2
    for call, (span, why) in zip(client.calls, targets):
        assert call["params"]["prefill"] == _patch_prefill(span)
        assert call["params"]["grammar"] == _PATCH_REMAINDER_GRAMMAR
        assert call["tool_choice"] == TOOLS["editor_apply_patch"]["choice"]
        # Shared stack: prefix + writer user msg + draft, then the per-finding prompt.
        assert call["messages"][-2] == {"role": "assistant", "content": draft}
        assert span in call["messages"][-1]["content"] and why in call["messages"][-1]["content"]
    assert len(debug) == 2


async def test_collect_prefill_patches_stops_on_abort():
    client = _FakeClient()
    client.is_aborted = True
    base = CachedBase(prefix=(), tools=(), model="m")
    patches, debug = await _collect_prefill_patches(
        client, base, {"role": "user", "content": "u"}, "draft", [("span", "why")], {}, None
    )
    assert patches == []
    assert debug == ["aborted mid-batch"]
