"""Contributed detectors as the pipeline sees them: resolution and one run per turn.

Two properties carry the whole design here. A detector resolves to *inactive*
unless the user explicitly ticked it, and it runs exactly once no matter how
many times the editor re-audits -- the report's own snippet filter is what keeps
the findings current across the rewrite loop.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from backend.analysis import ContributedFinding
from backend.core import Macros
from backend.pipeline.config import _resolve_pipeline_config
from backend.pipeline.passes.editor.editor import _run_audit_detectors
from backend.workflows import (
    AuditDetectorBinding,
    AuditDetectorRequest,
    AuditDetectorSpec,
    RegistrySnapshot,
)

KEY = "slop-scorer:slop"


class _Client:
    completion_mode = "chat"
    is_aborted = False


def _binding(key: str = KEY, invoke=None) -> AuditDetectorBinding:
    async def none(_request):
        return ()

    return AuditDetectorBinding(
        spec=AuditDetectorSpec(key=key, label="Model-scored slop", content_digest="d" * 64),
        invoke=invoke or none,
    )


def _snapshot(*bindings: AuditDetectorBinding) -> RegistrySnapshot:
    return RegistrySnapshot(
        generation=1,
        workflows=MappingProxyType({}),
        by_hook=MappingProxyType({}),
        digests=MappingProxyType({}),
        fragment_types=MappingProxyType({}),
        audit_detectors=MappingProxyType({b.key: b for b in bindings}),
    )


def _config(snapshot, toggles):
    return _resolve_pipeline_config(
        {
            "model_name": "m",
            "endpoint_url": "http://writer.local/v1",
            "completion_mode": "chat",
            "enabled_tools": {},
            "agent_model_name": "am",
            "editor_audit_toggles": toggles,
        },
        {},
        macros=Macros("User", ""),
        client=_Client(),
        agent_client=None,
        agent_prefix=None,
        prefix=[],
        phrase_bank=None,
        schema_overrides={},
        registry=snapshot,
    )


def _request(draft: str = "a draft") -> AuditDetectorRequest:
    return AuditDetectorRequest(
        draft=draft,
        previous_messages=(),
        conversation_id="c1",
        turn_seed="seed",
        settings={"model_name": "m"},
        client=_Client(),
        is_cancelled=lambda: False,
    )


# ── resolution: eligible on install, inert until ticked ──────────────────────


def test_a_detector_with_no_toggle_resolves_to_inactive():
    """``analysis.audit._on`` defaults a missing key to *enabled*, which is right
    for the built-in scanners shipped in schema.py's default JSON and wrong here:
    installing a package must not silently add a per-turn model call and a
    draft-shaped egress to every reply."""
    assert _config(_snapshot(_binding()), {}).audit_detectors == ()


def test_a_detector_toggled_off_resolves_to_inactive():
    assert _config(_snapshot(_binding()), {KEY: False}).audit_detectors == ()


def test_a_detector_toggled_on_resolves_to_active():
    cfg = _config(_snapshot(_binding()), {KEY: True})
    assert [b.key for b in cfg.audit_detectors] == [KEY]


def test_only_the_ticked_detectors_resolve():
    snapshot = _snapshot(_binding("a:one"), _binding("b:two"))
    cfg = _config(snapshot, {"a:one": True, "b:two": False})
    assert [b.key for b in cfg.audit_detectors] == ["a:one"]


def test_a_truthy_non_boolean_toggle_does_not_activate_a_detector():
    """``is True``, not truthiness: a legacy or hand-edited value must not be
    what turns a package's model call on."""
    assert _config(_snapshot(_binding()), {KEY: 1}).audit_detectors == ()


def test_no_registry_means_no_detectors():
    assert _config(None, {KEY: True}).audit_detectors == ()


# ── the batch: failure is always zero findings, never a failed turn ──────────


async def test_findings_from_every_detector_are_merged():
    async def one(_request):
        return (ContributedFinding(detector_id="a:one", label="A", snippet="x", note="n"),)

    async def two(_request):
        return (ContributedFinding(detector_id="b:two", label="B", snippet="y", note="n"),)

    findings = await _run_audit_detectors((_binding("a:one", one), _binding("b:two", two)), _request())
    assert [f.detector_id for f in findings] == ["a:one", "b:two"]


async def test_a_failing_detector_yields_no_findings_and_does_not_fail_the_turn():
    async def boom(_request):
        raise RuntimeError("revoked grant")

    async def fine(_request):
        return (ContributedFinding(detector_id="b:two", label="B", snippet="y", note="n"),)

    findings = await _run_audit_detectors((_binding("a:one", boom), _binding("b:two", fine)), _request())
    assert [f.detector_id for f in findings] == ["b:two"]


async def test_a_hung_detector_is_abandoned_at_the_timeout(monkeypatch):
    import asyncio

    import backend.pipeline.passes.editor.editor as editor_mod

    monkeypatch.setattr(editor_mod, "AUDIT_DETECTOR_TIMEOUT_SECONDS", 0.01)

    async def hang(_request):
        await asyncio.sleep(5)
        raise AssertionError("should not finish")

    assert await _run_audit_detectors((_binding("a:one", hang),), _request()) == ()


async def test_no_detectors_or_no_request_is_a_no_op():
    assert await _run_audit_detectors((), _request()) == ()
    assert await _run_audit_detectors((_binding(),), None) == ()


# ── once per turn ───────────────────────────────────────────────────────────


async def test_detectors_run_once_across_a_multi_iteration_editor_loop(monkeypatch):
    """The editor audits up to three times; re-running detectors per iteration
    would be up to 4x the model calls and 4x the KV-prefix evictions for findings
    that are mostly still valid."""
    import json

    from backend.analysis import AuditReport
    from backend.inference import CachedBase, LLMClient, enabled_schemas
    from backend.pipeline.passes.editor import editor as editor_mod

    detector_calls = 0
    audit_calls = 0

    async def counting(_request):
        nonlocal detector_calls
        detector_calls += 1
        return (ContributedFinding(detector_id=KEY, label="Slop", snippet="alpha", note="filler"),)

    # Never converges: every audit reports two built-in issues, so the loop runs
    # its full MAX_EDITOR_ITERATIONS and audits once more than that.
    async def fake_audit(_draft, _bank, _msgs, _toggles=None, _user=""):
        nonlocal audit_calls
        audit_calls += 1
        report = AuditReport.clean()
        report.not_but_result = [{"sentence": "not a, but b"}, {"sentence": "not c, but d"}]
        return report, "report"

    monkeypatch.setattr(editor_mod, "_run_contextual_audit", fake_audit)

    client = LLMClient("http://localhost:9999")
    replacements = iter(["beta", "gamma", "delta"])

    async def fake_complete(*_args, **_kwargs):
        yield {
            "type": "done",
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc",
                        "function": {
                            "name": "editor_apply_patch",
                            "arguments": json.dumps({"patches": [{"search": "alpha", "replace": next(replacements)}]}),
                        },
                    }
                ],
            },
        }

    client.complete = fake_complete
    base = CachedBase(
        prefix=({"role": "system", "content": "sys"},),
        tools=tuple(enabled_schemas({"editor_apply_patch": True}, {})),
        model="m",
    )

    events = [
        event
        async for event in editor_mod._run_edit_loop(
            client,
            base,
            "user said",
            "alpha",
            {},
            [],
            audit_enabled=True,
            detectors=(_binding(KEY, counting),),
            detector_request=_request(),
        )
    ]

    assert audit_calls > 1, "the loop must have re-audited at least once"
    assert detector_calls == 1, "detectors must run once per turn, not once per editor iteration"
    assert any(event["type"] == "done" for event in events)


@pytest.mark.parametrize("audit_enabled", [False])
async def test_detectors_do_not_run_when_the_audit_is_off(audit_enabled, monkeypatch):
    """No audit means no report to merge into, so the model call is pure cost."""
    from backend.inference import CachedBase
    from backend.pipeline.passes.editor import editor as editor_mod

    calls = 0

    async def counting(_request):
        nonlocal calls
        calls += 1
        return ()

    base = CachedBase(prefix=(), tools=(), model="m", resolve=lambda m: m)
    async for _event in editor_mod._run_edit_loop(
        _Client(),
        base,
        "user said",
        "a draft",
        {},
        [],
        audit_enabled=audit_enabled,
        detectors=(_binding(KEY, counting),),
        detector_request=_request(),
    ):
        pass
    assert calls == 0
