"""Community records reaching the manifest, the loader gate, and the tool blob.

Phase 0 publishes community *records* with no executable entry point. What has
to hold before that is safe: a community id must be visible to the manifest
without becoming a dynamic ``import()``, and its presence must not perturb the
model tool blob every pipeline pass shares.
"""

from __future__ import annotations

import pytest

from backend.inference import TOOLS, enabled_schemas
from backend.workflows import registry as reg
from backend.workflows.contracts import HookStage, HookType, LoadStatus, WorkflowSource
from backend.workflows.registry import Workflow, publish_community_overlay

from ._fixtures import _restore_registry  # noqa: F401 -- autouse fixture

pytestmark = pytest.mark.usefixtures("_restore_registry")


@pytest.fixture(autouse=True)
def _restore_overlay():
    published = reg._PUBLISHED
    yield
    reg._PUBLISHED = published


def _community(wid: str, **kwargs) -> Workflow:
    return Workflow(
        id=wid,
        display_name=kwargs.pop("display_name", wid),
        source=WorkflowSource.COMMUNITY,
        extension_api=1,
        content_digest="e" * 64,
        **kwargs,
    )


async def test_available_community_record_appears_as_declarative(client):
    publish_community_overlay([_community("scene-meter", display_name="Scene Meter")])
    body = (await client.get("/api/workflows")).json()
    entry = next(w for w in body if w["id"] == "scene-meter")
    assert entry["source"] == "community"
    # The loader gate: this is not "trusted_module", so the id never reaches
    # import('/static/workflows/<id>/index.js').
    assert entry["frontend_kind"] == "declarative"
    assert entry["extension_api"] == 1
    assert entry["load_status"] == "available"


async def test_unavailable_community_record_stays_visible_with_its_diagnostic(client):
    publish_community_overlay(
        [
            _community(
                "broken",
                load_status=LoadStatus.INCOMPATIBLE,
                diagnostic="requires operation 'quantum.entangle'",
            )
        ]
    )
    body = (await client.get("/api/workflows")).json()
    entry = next(w for w in body if w["id"] == "broken")
    assert entry["load_status"] == "incompatible"
    assert entry["diagnostic"] == "requires operation 'quantum.entangle'"
    assert entry["frontend_kind"] == "declarative"


async def test_disabled_community_record_stays_in_the_manifest(client):
    from backend.database import set_workflow_enabled

    publish_community_overlay([_community("scene-meter")])
    await set_workflow_enabled("scene-meter", False)
    body = (await client.get("/api/workflows")).json()
    # Enablement is settings state, never a manifest field: a disabled package
    # is still installed and still configurable.
    assert any(w["id"] == "scene-meter" for w in body)


async def test_no_community_record_is_ever_a_trusted_module(client):
    publish_community_overlay([_community("a"), _community("b", load_status=LoadStatus.INVALID, diagnostic="bad manifest")])
    body = (await client.get("/api/workflows")).json()
    trusted = {w["id"] for w in body if w["frontend_kind"] == "trusted_module"}
    assert trusted == {w["id"] for w in body if w["source"] == "builtin"}
    assert "a" not in trusted and "b" not in trusted


def test_enabling_a_community_record_does_not_change_the_tool_blob():
    """The KV-cache-relevant property: an ordinary extension adds no tools.

    Community flows make their own bounded model calls with an isolated
    request. Their schemas never enter TOOLS, PRE_WRITER_TOOLS,
    POST_WRITER_TOOLS, or any pass's CachedBase, so the shared prefix every
    pass reuses is byte-identical before and after an install.
    """
    enabled = {name: True for name in TOOLS}
    before_tools = dict(TOOLS)
    before_blob = enabled_schemas(enabled)

    publish_community_overlay([_community("scene-meter"), _community("api-artifact", produces_artifacts=False)])

    assert dict(TOOLS) == before_tools
    assert enabled_schemas(enabled) == before_blob


def test_community_hooks_are_ordered_after_builtins_within_a_stage():
    from backend.workflows import current_snapshot

    async def _hook(_ctx):
        if False:  # pragma: no cover
            yield

    record = _community("scene-meter")
    reg._bind_subscription(record, HookType.POST_PIPELINE, _hook, priority=-999, stage=HookStage.TRANSFORM)
    publish_community_overlay([record])

    order = [s.workflow_id for s in current_snapshot().subscriptions(HookType.POST_PIPELINE)]
    # format_consistency is the shipped transform, tts the shipped observer.
    assert order.index("format_consistency") < order.index("scene-meter")
    assert order.index("scene-meter") < order.index("tts")
