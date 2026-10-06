"""Check the plug-in seam with Pyright, including errors runtime tests cannot see.

The fixture is only type-checked, never imported. Each ``# rejected`` line must
produce an error; every other line must pass. Exact ``assert_type`` checks also
fail if a lookup silently erases a hook to Any or a broad Callable again.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

_SOURCE = """\
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import assert_type

from backend.inference import KVCacheTracker, LLMClient
from backend.workflows import get_subscription, iter_subscriptions, subscribe
from backend.workflows.contracts import (
    ExportHook, OnDemandHook, PostHook, PreHook, QueryHook, RegenHook,
    RerollGenHook, UploadHook, WorkflowHook,
)
from backend.workflows.toolkit import (
    EV_ATTACH_ARTIFACT, EV_DRAFT_REPLACED, EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE, EV_SYSTEM_PROMPT,
    HookType, PostCtx, PostEvent, PreCtx, PreEvent, PublicEvent, QueryCtx,
    Subscription, Workflow, WorkflowEventStream, subscription,
)

def declarations(
    pre: PreHook, post: PostHook, demand: OnDemandHook, regen: RegenHook,
    reroll: RerollGenHook, query: QueryHook, upload: UploadHook, export: ExportHook,
    slot: HookType,
) -> None:
    assert_type(subscription(HookType.PRE_PIPELINE, pre), Subscription[PreHook])
    assert_type(subscription(HookType.POST_PIPELINE, post), Subscription[PostHook])
    assert_type(subscription(HookType.ON_DEMAND, demand), Subscription[OnDemandHook])
    assert_type(subscription(HookType.REGENERATE, regen), Subscription[RegenHook])
    assert_type(subscription(HookType.REROLL_GEN, reroll), Subscription[RerollGenHook])
    assert_type(subscription(HookType.QUERY, query), Subscription[QueryHook])
    assert_type(subscription(HookType.UPLOAD, upload), Subscription[UploadHook])
    assert_type(subscription(HookType.EXPORT, export), Subscription[ExportHook])
    Workflow(id="example", display_name="Example", subscriptions=[
        subscription(HookType.PRE_PIPELINE, pre),
        subscription(HookType.POST_PIPELINE, post),
    ])
    subscription(HookType.PRE_PIPELINE, post)  # rejected
    subscribe("example", HookType.QUERY, pre)  # rejected
    assert_type(get_subscription("example", slot), Subscription[WorkflowHook] | None)
    assert_type(iter_subscriptions(slot), Sequence[Subscription[WorkflowHook]])

def lookups() -> None:
    assert_type(get_subscription("example", HookType.PRE_PIPELINE), Subscription[PreHook] | None)
    assert_type(get_subscription("example", HookType.POST_PIPELINE), Subscription[PostHook] | None)
    assert_type(get_subscription("example", HookType.ON_DEMAND), Subscription[OnDemandHook] | None)
    assert_type(get_subscription("example", HookType.REGENERATE), Subscription[RegenHook] | None)
    assert_type(get_subscription("example", HookType.REROLL_GEN), Subscription[RerollGenHook] | None)
    assert_type(get_subscription("example", HookType.QUERY), Subscription[QueryHook] | None)
    assert_type(get_subscription("example", HookType.UPLOAD), Subscription[UploadHook] | None)
    assert_type(get_subscription("example", HookType.EXPORT), Subscription[ExportHook] | None)
    assert_type(iter_subscriptions(HookType.PRE_PIPELINE), Sequence[Subscription[PreHook]])
    assert_type(iter_subscriptions(HookType.POST_PIPELINE), Sequence[Subscription[PostHook]])
    assert_type(iter_subscriptions(HookType.ON_DEMAND), Sequence[Subscription[OnDemandHook]])
    assert_type(iter_subscriptions(HookType.REGENERATE), Sequence[Subscription[RegenHook]])
    assert_type(iter_subscriptions(HookType.REROLL_GEN), Sequence[Subscription[RerollGenHook]])
    assert_type(iter_subscriptions(HookType.QUERY), Sequence[Subscription[QueryHook]])
    assert_type(iter_subscriptions(HookType.UPLOAD), Sequence[Subscription[UploadHook]])
    assert_type(iter_subscriptions(HookType.EXPORT), Sequence[Subscription[ExportHook]])

async def dispatch(pre: PreCtx, post: PostCtx, query: QueryCtx) -> None:
    for sub in iter_subscriptions(HookType.PRE_PIPELINE):
        assert_type(sub.callable(pre), AsyncIterator[PreEvent])
        sub.callable(post)  # rejected
    for sub in iter_subscriptions(HookType.POST_PIPELINE):
        assert_type(sub.callable(post), AsyncIterator[PostEvent])
        await sub.callable(post)  # rejected
    sub = get_subscription("example", HookType.QUERY)
    if sub is not None:
        await sub.callable(query, {})
        await sub.callable(pre, {})  # rejected
        await sub.callable(query)  # rejected
    assert_type(pre.client, LLMClient)
    assert_type(post.agent_client, LLMClient | None)
    assert_type(pre.kv_tracker, KVCacheTracker)
    assert_type(pre.enabled_tools_pre_merge, Mapping[str, bool])
    assert_type(post.enabled_tools, Mapping[str, bool])
    pre.enabled_tools_pre_merge["example"] = True  # rejected
    post.settings["model_name"] = "changed"  # rejected
    pre.client.nonexistent_method()  # rejected
    pre.kv_tracker.nonexistent_method()  # rejected

async def pre_events(ctx: PreCtx) -> AsyncIterator[PreEvent]:
    yield {"event": "phase_status", "data": {"custom": [1, 2]}}
    yield {"type": EV_ENABLE_TOOLS, "tools": {"example": True}}
    yield {"type": EV_ENABLE_TOOLS, "tools": {"example"}}
    yield {"type": EV_SYSTEM_PROMPT, "block": "instruction"}
    yield {"type": EV_DRAFT_REPLACED, "draft": "wrong slot"}  # rejected
    yield {"type": EV_SYSTEM_PROMPT, "block": 123}  # rejected
    yield {"type": EV_ENABLE_TOOLS, "tools": {"example": False}}  # rejected
    yield {"type": EV_SYSTEM_PROMPT, "blok": "misspelled"}  # rejected

async def post_events(ctx: PostCtx) -> AsyncIterator[PostEvent]:
    yield {"type": EV_DRAFT_REPLACED, "draft": "replacement"}
    yield {"type": EV_ATTACH_ARTIFACT, "attachment": {"custom": "payload"}}
    yield {"type": EV_SET_MESSAGE_STATE, "state": {"custom": "state"}}
    yield {"event": "custom_event"}
    yield {"type": EV_ENABLE_TOOLS, "tools": {"example"}}  # rejected
    yield {"type": EV_DRAFT_REPLACED}  # rejected
    yield {"type": "draft_replced", "draft": "misspelled type"}  # rejected

async def public_events() -> AsyncIterator[PublicEvent]:
    yield {"event": "custom_event", "data": {"value": 1}}
    yield {"event": "custom_event", "data": "text"}
    yield {"event": "custom_event"}
    yield {"event": "custom_event", "data": [1, 2]}  # rejected
    yield {"type": EV_DRAFT_REPLACED, "draft": "private instruction"}  # rejected

WorkflowEventStream(events=public_events())
subscription(HookType.PRE_PIPELINE, pre_events)
subscription(HookType.POST_PIPELINE, post_events)
"""


def test_workflow_contracts_are_checked_at_declaration_lookup_and_dispatch(tmp_path):
    source = tmp_path / "workflow_contracts.py"
    source.write_text(_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--project", str(ROOT / "pyrightconfig.json"), "--outputjson", str(source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    errors = [d for d in json.loads(result.stdout)["generalDiagnostics"] if d["severity"] == "error"]
    expected = {i for i, line in enumerate(_SOURCE.splitlines()) if line.endswith("# rejected")}
    assert {d["range"]["start"]["line"] for d in errors if Path(d["file"]) == source} == expected, json.dumps(errors, indent=2)
    assert all(Path(d["file"]) == source for d in errors), json.dumps(errors, indent=2)
