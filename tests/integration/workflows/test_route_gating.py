"""Route-level gating for disabled workflows.

Production routes (regenerate / reroll-gen / rehydrate run a hook) 404 when the owning workflow is off, and do so *before*
taking the per-root lock the live activate/delete consumption routes share -- so a stale production request never contends with
them. Consumption routes stay open. The tool-union strip removes a disabled workflow's standalone=False tool from the per-turn
blob.
"""

from __future__ import annotations

from backend.api import deps
from backend.database import add_message, insert_workflow_attachment_row, set_active_leaf, set_workflow_enabled, update_settings
from backend.inference import LLMClient
from backend.pipeline.config import resolve_pipeline_config
from backend.workflows import ToolSpec
from backend.workflows.attachment_cache import evict

from ._fixtures import make_workflow, register_for_test


async def _new_conversation(client) -> str:
    return await client.create("/api/conversations", json={"title": "gating"})


async def _seed_conv_with_message(client) -> tuple[str, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene draft", 0)
    await set_active_leaf(cid, mid)
    return cid, mid


def _artifact_workflow(calls: list):
    async def regen(_ctx, _body):
        calls.append("regen")
        return [{"filename": "v.png", "mime": "image/png", "data": b"V"}]

    async def reroll(_ctx, _params, _seed):
        calls.append("reroll")
        return b"NEW"

    return make_workflow("img", regenerate=regen, reroll_gen=reroll, produces_artifacts=True)


async def test_regenerate_404_when_locally_disabled_no_lock_no_hook(client):
    cid, mid = await _seed_conv_with_message(client)
    aid = await insert_workflow_attachment_row(
        mid, {"filename": "x.png", "mime": "image/png", "data": b"DATA", "workflow_id": "img"}
    )
    calls: list[str] = []
    with register_for_test(_artifact_workflow(calls)):
        await set_workflow_enabled("img", False)
        deps._workflow_root_locks.clear()
        resp = await client.post(f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate", json={})

    assert resp.status_code == 404
    assert calls == [], "the regenerate hook must not run for a disabled workflow"
    assert deps._workflow_root_locks == {}, "the root lock must not be taken before the 404"


async def test_regenerate_404_when_globally_disabled(client):
    cid, mid = await _seed_conv_with_message(client)
    aid = await insert_workflow_attachment_row(
        mid, {"filename": "x.png", "mime": "image/png", "data": b"DATA", "workflow_id": "img"}
    )
    calls: list[str] = []
    with register_for_test(_artifact_workflow(calls)):
        await update_settings({"workflows_globally_enabled": False})
        resp = await client.post(f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate", json={})

    assert resp.status_code == 404
    assert calls == []


async def test_rehydrate_404_when_disabled_does_not_run_hook(client):
    cid, mid = await _seed_conv_with_message(client)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"ORIGINAL",
            "workflow_id": "img",
            "seed": "STORED-SEED",
            "generation_metadata": {"steps": 4},
        },
    )
    await evict(aid)  # data_b64 -> EVICTED_MARKER, so the route reaches the gate
    calls: list[str] = []
    with register_for_test(_artifact_workflow(calls)):
        await set_workflow_enabled("img", False)
        resp = await client.post(f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate", json={})

    assert resp.status_code == 404
    assert calls == [], "rehydrate must not re-run the generative hook for a disabled workflow"


async def test_access_route_unaffected_when_disabled(client):
    # Access reporting is consumption (no hook, no lock): it must keep working so
    # LRU eviction bookkeeping stays accurate even while the workflow is off.
    cid, mid = await _seed_conv_with_message(client)
    aid = await insert_workflow_attachment_row(
        mid, {"filename": "x.png", "mime": "image/png", "data": b"DATA", "workflow_id": "img"}
    )
    with register_for_test(_artifact_workflow([])):
        await set_workflow_enabled("img", False)
        resp = await client.post(f"/api/conversations/{cid}/workflow-attachments/access", json={"ids": [aid]})

    assert resp.status_code == 200
    assert resp.json()["recorded"] == 1


# -- tool-union strip (3.5) --------------------------------------------------

_BASE_SETTINGS = {
    "model_name": "test",
    "enable_agent": 1,
    "reasoning_enabled_passes": {},
    "length_guard_enabled": 0,
    "length_guard_enforce": 0,
    "length_guard_max_words": 240,
    "length_guard_max_paragraphs": 4,
    "workflows_globally_enabled": 1,
}


class _StubMacros:
    # resolve_pipeline_config stores macros.resolve_prompt_messages on the lane's
    # CachedBase but does not call it during config resolution.
    def resolve_prompt_messages(self, *args, **kwargs):
        return []


def _resolve(settings, enabled_tools):
    return resolve_pipeline_config(
        settings,
        enabled_tools,
        macros=_StubMacros(),
        client=LLMClient("http://localhost:9999"),
        agent_client=None,
        agent_prefix=None,
        prefix=[{"role": "system", "content": "x"}],
        phrase_bank=None,
        schema_overrides={},
    )


def _blob_names(cfg) -> list[str]:
    return [s["function"]["name"] for s in cfg.writer_lane.base.tools]


async def test_disabled_workflow_tool_stripped_from_pipeline_blob():
    probe = make_workflow(
        "probe",
        tools=[
            ToolSpec(
                name="probe_tool",
                schema={
                    "type": "function",
                    "function": {"name": "probe_tool", "description": "p", "parameters": {"type": "object", "properties": {}}},
                },
                choice={"type": "function", "function": {"name": "probe_tool"}},
                standalone=False,
            )
        ],
    )
    enabled_tools = {"probe_tool": True}
    with register_for_test(probe):
        # A standing enabled_tools entry survives disabling, so the strip is the only
        # thing keeping a disabled workflow's tool out of the blob.
        cfg_on = _resolve({**_BASE_SETTINGS, "workflow_enabled": {}}, dict(enabled_tools))
        cfg_off = _resolve({**_BASE_SETTINGS, "workflow_enabled": {"probe": False}}, dict(enabled_tools))

    assert "probe_tool" in _blob_names(cfg_on)
    assert "probe_tool" in cfg_on.enabled_tools
    assert "probe_tool" not in _blob_names(cfg_off)
    assert "probe_tool" not in cfg_off.enabled_tools
