"""Registry snapshots: built-in base, community overlay, and hook ordering.

The snapshot exists so a turn resolves its hook set exactly once. These tests
pin the two properties that makes load-bearing: a captured snapshot does not
change when the registry does, and the resolved order is a function of the
package set rather than of installation time.
"""

from __future__ import annotations

import pytest

from backend.workflows import registry as reg
from backend.workflows.contracts import HookStage, HookType, LoadStatus, WorkflowSource
from backend.workflows.registry import (
    Workflow,
    WorkflowDeclarationError,
    WorkflowMandateError,
    current_snapshot,
    publish_community_overlay,
    runtime_generation,
)


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Snapshot and restore both bands so a test cannot leak into its neighbours."""
    base = dict(reg._WORKFLOWS_BY_ID)
    published = reg._PUBLISHED
    reg._WORKFLOWS_BY_ID.clear()
    reg._PUBLISHED = reg._Published(generation=published.generation)
    yield
    reg._WORKFLOWS_BY_ID.clear()
    reg._WORKFLOWS_BY_ID.update(base)
    reg._PUBLISHED = published


async def _noop_hook(_ctx):
    if False:  # pragma: no cover -- async generator with no yields
        yield


def community(wid: str, **kwargs) -> Workflow:
    record = Workflow(
        id=wid,
        display_name=kwargs.pop("display_name", wid),
        source=WorkflowSource.COMMUNITY,
        extension_api=kwargs.pop("extension_api", 1),
        content_digest=kwargs.pop("content_digest", "d" * 64),
        **kwargs,
    )
    return record


def bind(record: Workflow, hook: HookType, *, priority: int = 0, stage: HookStage = HookStage.TRANSFORM) -> Workflow:
    reg._bind_subscription(record, hook, _noop_hook, priority=priority, stage=stage)
    return record


def builtin(wid: str) -> Workflow:
    record = Workflow(id=wid, display_name=wid)
    reg.register_workflow(record)
    return record


# ── trust tiers ──────────────────────────────────────────────────────────────


def test_builtins_are_trusted_modules_and_community_is_declarative():
    # frontend_kind is derived, not stored: there is no field an install path
    # could set to move a package into the band the frontend import()s.
    assert builtin("core").frontend_kind.value == "trusted_module"
    assert community("scene-meter").frontend_kind.value == "declarative"


def test_register_workflow_refuses_a_community_record():
    with pytest.raises(WorkflowDeclarationError, match="publish_community_overlay"):
        reg.register_workflow(community("scene-meter"))


def test_community_record_cannot_declare_tools():
    from backend.workflows.contracts import ToolSpec

    schema = {"type": "function", "function": {"name": "evil"}}
    record = community("scene-meter", tools=[ToolSpec("evil", schema, schema)])
    with pytest.raises(WorkflowDeclarationError, match="never enter the shared tool registry"):
        publish_community_overlay([record])


def test_community_id_cannot_collide_with_a_builtin():
    builtin("tts")
    with pytest.raises(WorkflowDeclarationError, match="collides with a built-in"):
        publish_community_overlay([community("tts")])


def test_reserved_ids_are_refused():
    with pytest.raises(WorkflowDeclarationError, match="reserved"):
        publish_community_overlay([community("macros")])


def test_duplicate_community_ids_are_refused():
    with pytest.raises(WorkflowDeclarationError, match="duplicate community"):
        publish_community_overlay([community("a"), community("a")])


def test_community_ids_must_use_the_lowercase_extension_grammar():
    # Stricter than the built-in id grammar on purpose. The id is also a state
    # slot key and a content-store path component, so a case variant must not be
    # a second installable package.
    with pytest.raises(WorkflowDeclarationError, match="lowercase extension id grammar"):
        publish_community_overlay([community("Scene-Meter")])


def test_unavailable_record_may_not_publish_entry_points():
    record = bind(community("broken", load_status=LoadStatus.INVALID), HookType.POST_PIPELINE)
    with pytest.raises(WorkflowDeclarationError, match="published entry points"):
        publish_community_overlay([record])


def test_unavailable_record_may_not_declare_artifact_production():
    # produces_artifacts is an entry-point declaration too. Left set on an
    # unavailable record it would trip the artifact mandate and fail the whole
    # overlay swap -- one broken package taking every other extension with it.
    record = community("broken", load_status=LoadStatus.MISSING_CONTENT, produces_artifacts=True)
    with pytest.raises(WorkflowDeclarationError, match="published entry points"):
        publish_community_overlay([record])


def test_one_unavailable_package_does_not_block_the_rest_of_the_overlay():
    healthy = bind(community("healthy"), HookType.POST_PIPELINE)
    broken = community("broken", load_status=LoadStatus.MISSING_CONTENT, diagnostic="content missing")
    publish_community_overlay([healthy, broken])
    snap = current_snapshot()
    assert [s.workflow_id for s in snap.subscriptions(HookType.POST_PIPELINE)] == ["healthy"]
    assert snap.get("broken").load_status is LoadStatus.MISSING_CONTENT


def test_unavailable_record_without_hooks_is_published_and_visible():
    # Installed, inspectable, diagnosable -- and contributing nothing. That is
    # the difference between "unavailable" and "gone".
    record = community("broken", load_status=LoadStatus.INCOMPATIBLE, diagnostic="requires a newer Orb")
    publish_community_overlay([record])
    snap = current_snapshot()
    assert snap.get("broken") is not None
    assert snap.get("broken").diagnostic == "requires a newer Orb"
    assert snap.subscriptions(HookType.POST_PIPELINE) == ()


def test_community_artifact_mandate_is_enforced_before_publish():
    record = community("art", produces_artifacts=True)
    bind(record, HookType.REGENERATE)
    with pytest.raises(WorkflowMandateError, match="reroll_gen"):
        publish_community_overlay([record])


def test_a_rejected_publish_leaves_the_prior_overlay_active():
    publish_community_overlay([community("good")])
    generation = runtime_generation()
    with pytest.raises(WorkflowDeclarationError):
        publish_community_overlay([community("macros")])
    assert current_snapshot().get("good") is not None
    assert runtime_generation() == generation


# ── generations ──────────────────────────────────────────────────────────────


def test_every_publish_advances_the_generation():
    first = publish_community_overlay([])
    second = publish_community_overlay([community("a")])
    assert second > first
    assert current_snapshot().generation == second


def test_generation_and_overlay_are_published_as_one_value():
    # Generation and overlay are one fact, held in one immutable reference. Two
    # globals could only be written one at a time, and a reader landing between
    # the writes would get the new label over the old records.
    publish_community_overlay([community("a")])
    before = reg._PUBLISHED
    publish_community_overlay([community("b")])
    after = reg._PUBLISHED
    assert before.generation != after.generation
    assert [w.id for w in before.overlay] == ["a"]
    assert [w.id for w in after.overlay] == ["b"]
    # Every generation the registry ever reported names exactly one record set.
    assert reg.runtime_generation() == after.generation
    assert current_snapshot().generation == after.generation


def test_bump_generation_advances_without_changing_records():
    publish_community_overlay([community("a")])
    before = current_snapshot()
    generation = reg.bump_generation()
    after = current_snapshot()
    assert generation > before.generation
    assert [w.id for w in after.list()] == [w.id for w in before.list()]


def test_a_captured_snapshot_is_unaffected_by_a_later_publish():
    # The whole point: a turn holds one snapshot, and an install landing
    # mid-turn changes the next turn, not this one.
    publish_community_overlay([bind(community("first"), HookType.POST_PIPELINE)])
    captured = current_snapshot()

    publish_community_overlay([bind(community("second"), HookType.POST_PIPELINE)])

    assert [s.workflow_id for s in captured.subscriptions(HookType.POST_PIPELINE)] == ["first"]
    assert [s.workflow_id for s in current_snapshot().subscriptions(HookType.POST_PIPELINE)] == ["second"]
    assert captured.get("second") is None


def test_snapshot_mappings_are_read_only():
    publish_community_overlay([community("a")])
    snap = current_snapshot()
    with pytest.raises(TypeError):
        snap.workflows["b"] = community("b")


def test_snapshot_digests_track_the_active_revision():
    publish_community_overlay([community("a", content_digest="a" * 64)])
    assert current_snapshot().digests == {"a": "a" * 64}


# ── hook ordering ────────────────────────────────────────────────────────────


def test_transform_stage_precedes_observe_across_source_bands():
    # Stage beats source band: every community transform runs before every
    # trusted observer, which is what lets a built-in observer (TTS) rely on
    # seeing the finished text.
    trusted_observer = builtin("trusted-observer")
    bind(trusted_observer, HookType.POST_PIPELINE, stage=HookStage.OBSERVE)
    trusted_transform = builtin("trusted-transform")
    bind(trusted_transform, HookType.POST_PIPELINE, stage=HookStage.TRANSFORM, priority=50)
    publish_community_overlay([bind(community("z-community"), HookType.POST_PIPELINE, stage=HookStage.TRANSFORM)])

    order = [s.workflow_id for s in current_snapshot().subscriptions(HookType.POST_PIPELINE)]
    assert order == ["trusted-transform", "z-community", "trusted-observer"]


def test_builtins_form_the_first_source_band_within_a_stage():
    # Even with a worse priority, a built-in runs before a community entry in
    # the same stage: the bands are ordered, then sorted within.
    late_builtin = builtin("late-builtin")
    bind(late_builtin, HookType.PRE_PIPELINE, priority=100)
    publish_community_overlay([bind(community("early-community"), HookType.PRE_PIPELINE, priority=-100)])

    order = [s.workflow_id for s in current_snapshot().subscriptions(HookType.PRE_PIPELINE)]
    assert order == ["late-builtin", "early-community"]


def test_builtin_priority_then_registration_order():
    for wid in ("a", "b", "c"):
        bind(builtin(wid), HookType.PRE_PIPELINE)
    bind(builtin("first"), HookType.PRE_PIPELINE, priority=-1)
    order = [s.workflow_id for s in current_snapshot().subscriptions(HookType.PRE_PIPELINE)]
    assert order == ["first", "a", "b", "c"]


def test_community_order_is_by_priority_then_id_not_publish_order():
    # Two users with the same packages must get the same turn, whatever order
    # they happened to install them in.
    records = [
        bind(community("zulu"), HookType.PRE_PIPELINE),
        bind(community("alpha"), HookType.PRE_PIPELINE),
        bind(community("mike"), HookType.PRE_PIPELINE, priority=-5),
    ]
    publish_community_overlay(records)
    first = [s.workflow_id for s in current_snapshot().subscriptions(HookType.PRE_PIPELINE)]

    publish_community_overlay(list(reversed(records)))
    second = [s.workflow_id for s in current_snapshot().subscriptions(HookType.PRE_PIPELINE)]

    assert first == ["mike", "alpha", "zulu"] == second


def test_community_records_carry_their_source_onto_subscriptions():
    publish_community_overlay([bind(community("a"), HookType.PRE_PIPELINE)])
    (sub,) = current_snapshot().subscriptions(HookType.PRE_PIPELINE)
    assert sub.source is WorkflowSource.COMMUNITY


def test_single_dispatch_lookup_resolves_against_the_snapshot():
    publish_community_overlay([bind(community("a"), HookType.ON_DEMAND)])
    snap = current_snapshot()
    assert snap.subscription("a", HookType.ON_DEMAND) is not None
    assert snap.subscription("a", HookType.QUERY) is None
    assert snap.subscription("ghost", HookType.ON_DEMAND) is None


def test_list_puts_builtins_before_community_records():
    builtin("b1")
    builtin("b2")
    publish_community_overlay([community("c1")])
    assert [w.id for w in current_snapshot().list()] == ["b1", "b2", "c1"]
