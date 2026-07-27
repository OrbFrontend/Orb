"""Post-pipeline transform/observe staging in the workflow bridge.

Transforms may replace the draft and feed the next one; observers see a single
final draft. The bridge enforces the second half rather than trusting a hook to
know its own stage -- the ordering guarantee the *other* hooks rely on was
fixed when the snapshot was resolved, so a runtime opt-out would silently
invalidate it.
"""

from __future__ import annotations

from backend.pipeline.workflow_bridge import _PostPipelineResult, _run_post_pipeline
from backend.workflows import EV_DRAFT_REPLACED, HookStage, HookType, current_snapshot
from backend.workflows import registry as reg

from ._fixtures import _restore_registry, make_workflow  # noqa: F401 -- autouse fixture

_SETTINGS = {"model_name": "test"}


def _replacer(text: str):
    async def hook(_ctx):
        yield {"type": EV_DRAFT_REPLACED, "draft": text}

    return hook


def _recorder(sink: list):
    async def hook(ctx):
        sink.append(ctx.draft)
        if False:  # pragma: no cover -- async generator with no yields
            yield

    return hook


async def _drain(**kwargs):
    events, result = [], None
    async for ev in _run_post_pipeline(
        draft=kwargs.pop("draft", "original"),
        conversation_id="c1",
        character_id=None,
        card=None,
        history=[],
        effective_msg="hi",
        director_output={},
        settings=_SETTINGS,
        prefix=[],
        enabled_tools={},
        turn_scratch={},
        client=None,
        kv_tracker=None,
        schema_overrides={},
        registry=current_snapshot(),
        **kwargs,
    ):
        if isinstance(ev, _PostPipelineResult):
            result = ev
        else:
            events.append(ev)
    return events, result


def _register(wid: str, hook, *, stage: HookStage, priority: int = 0):
    record = make_workflow(wid)
    reg.register_workflow(record)
    reg._bind_subscription(record, HookType.POST_PIPELINE, hook, priority=priority, stage=stage)


async def test_transform_hook_replaces_the_draft():
    _register("t1", _replacer("rewritten"), stage=HookStage.TRANSFORM)
    events, result = await _drain()
    assert result.draft == "rewritten"
    assert [e["event"] for e in events] == ["writer_rewrite"]


async def test_observe_hook_cannot_replace_the_draft():
    _register("o1", _replacer("hijacked"), stage=HookStage.OBSERVE)
    events, result = await _drain()
    assert result.draft == "original"
    assert events == []


async def test_each_transform_feeds_the_next():
    _register("t1", _replacer("first"), stage=HookStage.TRANSFORM, priority=-10)

    async def second(ctx):
        yield {"type": EV_DRAFT_REPLACED, "draft": ctx.draft + "+second"}

    _register("t2", second, stage=HookStage.TRANSFORM, priority=0)
    _, result = await _drain()
    assert result.draft == "first+second"


async def test_every_observer_sees_the_same_final_draft():
    seen_a: list[str] = []
    seen_b: list[str] = []
    _register("t1", _replacer("final"), stage=HookStage.TRANSFORM, priority=-10)
    _register("o1", _recorder(seen_a), stage=HookStage.OBSERVE, priority=0)
    _register("o2", _recorder(seen_b), stage=HookStage.OBSERVE, priority=99)
    _, result = await _drain()
    assert seen_a == seen_b == ["final"]
    assert result.draft == "final"


async def test_a_failing_transform_does_not_discard_an_earlier_one():
    _register("t1", _replacer("kept"), stage=HookStage.TRANSFORM, priority=-10)

    async def boom(_ctx):
        raise RuntimeError("hook exploded")
        yield  # pragma: no cover

    _register("t2", boom, stage=HookStage.TRANSFORM, priority=0)
    seen: list[str] = []
    _register("o1", _recorder(seen), stage=HookStage.OBSERVE)

    _, result = await _drain()
    assert result.draft == "kept"
    assert seen == ["kept"]


async def test_observers_run_after_transforms_regardless_of_priority():
    # An observer with a much lower priority still runs second: stage precedes
    # priority, which is what keeps a consumer from reading a pre-rewrite draft.
    seen: list[str] = []
    _register("o1", _recorder(seen), stage=HookStage.OBSERVE, priority=-1000)
    _register("t1", _replacer("rewritten"), stage=HookStage.TRANSFORM, priority=1000)
    _, result = await _drain()
    assert seen == ["rewritten"]
    assert result.draft == "rewritten"
