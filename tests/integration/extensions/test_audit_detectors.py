"""Audit detectors end to end: install, tick, one run, one report section.

The unit suites cover the contract (what a v3 manifest may say), the value type
(what a finding is), and the resolution rule (default off). This one drives the
real routes and the real turn handler, because everything interesting lives in
the seams: a tick in the audit panel has to reach a captured snapshot, the
snapshot's binding has to reach the Editor, and the flow's findings have to reach
the report the Editor sends -- without any of it reaching persisted prose or the
turn's shared prefix.
"""

from __future__ import annotations

import backend.database as dbmod
from backend.features.extensions.runtime import current_state
from backend.pipeline import handle_turn
from backend.pipeline.config import _resolve_pipeline_config
from backend.workflows.registry import current_snapshot
from tests.extension_packages import SLOP_SCORER_ID, audit_detector_package

from .conftest import catalog, entry, install

KEY = f"{SLOP_SCORER_ID}:slop"

SCORE_CALL = {
    "tool_calls": [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "extension_output",
                "arguments": {"snippet": "She smiles softly.", "note": "generic reaction beat"},
            },
        }
    ]
}
"""What the mock returns for the detector's ``model.structured`` step.

``extension_output`` is host-owned, exactly as in the Scene Meter fixture: a
package contributes the schema, never the name."""


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _conversation(cid: str = "conv-det") -> str:
    await dbmod.create_conversation(cid, "det", "Bot", "a scenario")
    return cid


async def _install(client, *, package: bytes | None = None, enabled: bool = True, tick: bool = True):
    # Single-model, like the Writer-tool suite: it keeps assertions about the
    # one shared prefix honest.
    await client.put("/api/settings", json={"enable_agent": False})
    await install(client, package or audit_detector_package(), enabled=enabled)
    if tick:
        response = await client.put("/api/settings", json={"editor_audit_toggles": {KEY: True}})
        assert response.status_code == 200, response.text


async def _cfg(client, cid: str):
    """The per-turn config a real turn would resolve, for the detector list."""
    settings = (await client.get("/api/settings")).json()
    from backend.core import Macros

    class _Client:
        completion_mode = "chat"

    return _resolve_pipeline_config(
        {**settings, "model_name": settings.get("model_name") or "m"},
        {},
        macros=Macros("User", ""),
        client=_Client(),
        agent_client=None,
        agent_prefix=None,
        prefix=[],
        phrase_bank=None,
        schema_overrides={},
        registry=current_snapshot(),
    )


# ── publishing and the toggle ───────────────────────────────────────────────


async def test_installing_publishes_the_binding_but_not_the_activation(client):
    """Availability is not activation, exactly as with the Writer tool. Both
    come from the same snapshot, and installing decides only the first."""
    await _install(client, tick=False)
    assert KEY in current_snapshot().audit_detectors
    assert (await _cfg(client, "c")).audit_detectors == ()


async def test_the_catalog_lists_the_detector_off_so_the_panel_can_render_it(client):
    await _install(client, tick=False)
    rows = (await entry(client, SLOP_SCORER_ID))["audit_detectors"]
    assert rows == [
        {
            "id": "slop",
            "namespaced_id": KEY,
            "label": "Model-scored slop",
            "description": "Flags the weakest sentence in each reply.",
            "enabled": False,
        }
    ]


async def test_ticking_the_toggle_activates_the_detector_for_a_turn(client):
    await _install(client)
    assert [b.key for b in (await _cfg(client, "c")).audit_detectors] == [KEY]
    assert (await entry(client, SLOP_SCORER_ID))["audit_detectors"][0]["enabled"] is True


async def test_disabling_the_package_unpublishes_the_detector(client):
    """The tick survives (it is a preference), but a disabled package must not
    run a model call for every reply."""
    await _install(client)
    await client.post(f"/api/extensions/{SLOP_SCORER_ID}/enabled", json={"enabled": False})

    assert current_snapshot().audit_detectors == {}
    assert (await _cfg(client, "c")).audit_detectors == ()
    assert (await client.get("/api/settings")).json()["editor_audit_toggles"][KEY] is True


async def test_revoking_the_grant_unpublishes_the_detector(client):
    await _install(client)
    await client.put(
        f"/api/extensions/{SLOP_SCORER_ID}/permissions",
        json={"permissions": [{"capability": "context.read", "field": "draft"}]},
    )
    assert "audit detectors" in current_state().get(SLOP_SCORER_ID).blocked
    assert current_snapshot().audit_detectors == {}


async def test_a_v2_package_offers_no_detector_rows(client):
    from tests.extension_packages import outcome_resolver_package

    await install(client, outcome_resolver_package())
    assert (await entry(client, "outcome-resolver"))["audit_detectors"] == []


async def test_an_uninstalled_package_leaves_no_detector_behind(client):
    await _install(client)
    await client.delete(f"/api/extensions/{SLOP_SCORER_ID}")
    assert current_snapshot().audit_detectors == {}
    assert (await catalog(client))["extensions"] == []


# ── the turn ────────────────────────────────────────────────────────────────


# One built-in finding rides along so the report crosses the editor's
# "more than one issue" threshold and the LLM loop actually runs -- contrastive
# negation needs no phrase bank, unlike the banned-phrase scanner.
NOT_BUT_DRAFT = "She smiles softly. It was not anger, but grief."


async def _enable_audit(client):
    """The Output Auditor needs the agent on: ``audit_enabled`` is
    ``agent_on and editor_apply_patch and a phrase bank``."""
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"editor_apply_patch": True}})


async def _turn_with_audit(client, llm_mock, cid: str, draft: str = NOT_BUT_DRAFT):
    """Drive one turn with the Output Auditor on and a scripted detector call."""
    await _enable_audit(client)
    llm_mock.enqueue_writer(draft)
    llm_mock.enqueue_workflow(SCORE_CALL)
    llm_mock.enqueue_editor()
    return await _drain(handle_turn(cid, "hello"))


async def test_a_disabled_detector_makes_no_model_call_during_a_turn(client, llm_mock):
    await _install(client, tick=False)
    cid = await _conversation("conv-det-off")
    await _enable_audit(client)
    llm_mock.enqueue_writer(NOT_BUT_DRAFT)
    llm_mock.enqueue_editor()

    await _drain(handle_turn(cid, "hello"))

    assert "workflow" not in [p for p, _ in llm_mock.calls]


async def test_an_enabled_detector_runs_once_and_lands_in_the_report(client, llm_mock):
    """The whole seam in one assertion: the tick reached the snapshot, the
    snapshot's binding reached the Editor, the flow made its isolated model
    call, and the finding reached the report the Editor was given."""
    await _install(client)
    cid = await _conversation("conv-det-on")
    await _turn_with_audit(client, llm_mock, cid)

    # Exactly one workflow-lane call: the detector's, not one per editor pass.
    assert [p for p, _ in llm_mock.calls].count("workflow") == 1

    editor_calls = [c for c in llm_mock.captured if c["pass"] == "editor"]
    assert editor_calls, "the editor pass did not run"
    report = str(editor_calls[0]["messages"][-1]["content"])
    assert "Model-scored slop" in report
    assert "generic reaction beat" in report


async def test_the_detector_call_leaves_the_shared_prefix_byte_identical(client, llm_mock):
    """The KV-cache rule: a finding renders into the Editor's per-turn tail
    message, exactly where the built-in report already goes. No pass's shared
    prefix gains a byte.

    Reuse is a separate question -- a model-calling detector issues one
    prefix-free completion, which on a single-slot server costs one eviction --
    but prefix *assembly* is the assertable half, and the llm_mock teardown
    checks it across every captured call.
    """
    await _install(client, tick=False)
    cid = await _conversation("conv-det-prefix")
    await _turn_with_audit(client, llm_mock, cid)
    without = {c["messages"][0]["content"] for c in llm_mock.captured if c["pass"] in ("writer", "editor")}

    llm_mock.captured.clear()
    llm_mock.calls.clear()
    await client.put("/api/settings", json={"editor_audit_toggles": {KEY: True}})
    await _turn_with_audit(client, llm_mock, cid)
    with_detector = {c["messages"][0]["content"] for c in llm_mock.captured if c["pass"] in ("writer", "editor")}

    assert len(with_detector) == 1, "the passes disagreed about the shared prefix"
    assert without == with_detector

    # The unavoidable half, recorded rather than asserted away: the detector's
    # own call is one isolated, prefix-free completion, so a single-slot server
    # pays one eviction between the Writer and the Editor.
    assert [p for p, _ in llm_mock.calls].count("workflow") == 1


async def test_a_detector_that_fails_leaves_the_turn_intact(client, llm_mock):
    """Any failure is zero findings, never a failed reply -- the same philosophy
    as RESOLVER_UNAVAILABLE. Here the flow's model call has no queued response."""
    await _install(client)
    cid = await _conversation("conv-det-fail")
    await _enable_audit(client)
    llm_mock.enqueue_writer(NOT_BUT_DRAFT)
    llm_mock.enqueue_editor()

    await _drain(handle_turn(cid, "hello"))

    messages = await dbmod.get_messages(cid)
    assert messages[-1]["content"].startswith("She smiles softly.")
