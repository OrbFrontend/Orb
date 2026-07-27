"""Fan-out suspension: a disabled workflow's PRE/POST hooks do not fire.

Drives the bridge iterators directly with probe hooks and varying settings. The
gate reads the settings snapshot threaded into each seam, so global-off suppresses
every workflow and local-off suppresses exactly one. The last test pins the 3.9
contract: format_consistency now carries no config gate of its own, so the
framework toggle is its sole on/off.
"""

from __future__ import annotations

from backend.inference import _KVCacheTracker
from backend.pipeline.workflow_bridge import (
    _iterate_pre_pipeline_hooks,
    _PostPipelineResult,
    _run_post_pipeline,
)
from backend.workflows import current_snapshot

from ._fixtures import make_workflow, register_for_test

_PREFIX = [{"role": "system", "content": "x"}]

# A quoted-convention baseline an asterisk-narration draft drifts from, so the real
# format_consistency hook rewrites it when it runs.
QUOTED_BASELINE = 'She smiles. "Hello there," she says warmly.'
DRIFTING_DRAFT = "*She steps closer, watching him carefully.* Are you sure about this?"


async def _pre_events(settings) -> list[dict]:
    accumulators = {"merged_enabled_tools": {}, "extras": []}
    events = []
    async for ev in _iterate_pre_pipeline_hooks(
        conversation_id="c1",
        history=[],
        last_user_message="hi",
        settings=settings,
        prefix_base=_PREFIX,
        enabled_tools_pre_merge={},
        turn_scratch={},
        client=None,
        kv_tracker=_KVCacheTracker(),
        schema_overrides={},
        accumulators=accumulators,
        registry=current_snapshot(),
    ):
        events.append(ev)
    return events


async def _post_event_names(settings, *, draft="draft", history=None) -> list[str]:
    names = []
    async for ev in _run_post_pipeline(
        draft=draft,
        conversation_id="c1",
        character_id=None,
        card=None,
        history=history or [],
        effective_msg="msg",
        director_output={},
        settings=settings,
        prefix=_PREFIX,
        enabled_tools={},
        turn_scratch={},
        client=None,
        kv_tracker=_KVCacheTracker(),
        schema_overrides={},
        registry=current_snapshot(),
    ):
        if not isinstance(ev, _PostPipelineResult):
            names.append(ev.get("event"))
    return names


async def test_pre_global_off_suppresses_every_workflow():
    async def hook(_ctx):
        yield {"event": "probe_fired"}

    w = make_workflow("probe", pre_pipeline=hook)
    with register_for_test(w):
        on = await _pre_events({"model_name": "test"})
        off = await _pre_events({"model_name": "test", "workflows_globally_enabled": 0})

    assert {"event": "probe_fired"} in on
    assert off == []


async def test_pre_local_off_suppresses_only_the_named_workflow():
    async def hook_a(_ctx):
        yield {"event": "a_fired"}

    async def hook_b(_ctx):
        yield {"event": "b_fired"}

    wa = make_workflow("wa", pre_pipeline=hook_a)
    wb = make_workflow("wb", pre_pipeline=hook_b)
    with register_for_test(wa), register_for_test(wb):
        events = await _pre_events({"model_name": "test", "workflow_enabled": {"wa": False}})

    names = [e.get("event") for e in events]
    assert "a_fired" not in names
    assert "b_fired" in names


async def test_post_local_off_suppresses_probe():
    async def hook(_ctx):
        yield {"event": "probe_post"}

    w = make_workflow("probe", post_pipeline=hook)
    with register_for_test(w):
        on = await _post_event_names({"model_name": "test"})
        off = await _post_event_names({"model_name": "test", "workflow_enabled": {"probe": False}})

    assert "probe_post" in on
    assert "probe_post" not in off


async def test_format_consistency_runs_when_enabled_and_is_suppressed_when_toggled_off():
    # The real format_consistency workflow is registered at import; its only on/off
    # is now the framework toggle (no config gate). writer_rewrite is its signature
    # event (the draft_replaced the bridge turns into an SSE rewrite).
    history = [{"role": "assistant", "content": QUOTED_BASELINE}]
    on = await _post_event_names({"model_name": "test"}, draft=DRIFTING_DRAFT, history=history)
    off = await _post_event_names(
        {"model_name": "test", "workflow_enabled": {"format_consistency": False}},
        draft=DRIFTING_DRAFT,
        history=history,
    )

    assert "writer_rewrite" in on
    assert "writer_rewrite" not in off
