"""Character-card snapshot (``ctx.character``) delivery to workflow hooks.

Pins that each in-scope hook context (PreCtx, PostCtx, OnDemandCtx,
RegenCtx) receives a read-only snapshot of the conversation's character
card and its character_id, and that both degrade to None when the
conversation has no card.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.kv_tracker import _KVCacheTracker
from backend.llm_client import LLMClient
from backend.orchestrator import _iterate_pre_pipeline_hooks, _run_pipeline

from ._fixtures import make_workflow, register_for_test

_PREFIX = [{"role": "system", "content": "sys"}]
_SETTINGS = {"model_name": "test", "enable_agent": 1, "enabled_tools": {}, "reasoning_enabled_passes": {}}
_DIRECTOR_STATE = {"active_moods": []}
_CARD = {"id": "c1", "name": "Aria", "description": "d", "personality": "warm", "tags": ["a", "b"]}


async def _drain(gen) -> list:
    return [e async for e in gen]


def _pipeline_kwargs() -> dict:
    return {
        "prefix": _PREFIX,
        "enabled_tools": {},
        "turn_scratch": {},
        "kv_tracker": _KVCacheTracker(),
        "schema_overrides": {},
    }


async def test_pre_pipeline_ctx_carries_readonly_card_snapshot():
    captured = {}

    async def pre_hook(pre_ctx):
        captured["character"] = pre_ctx.character
        captured["character_id"] = pre_ctx.character_id
        yield {"event": "noop", "data": {}}

    w = make_workflow("cf_pre", pre_pipeline=pre_hook)
    with register_for_test(w):
        await _drain(
            _iterate_pre_pipeline_hooks(
                conversation_id="conv",
                character_id="c1",
                card=dict(_CARD),
                history=[],
                last_user_message="hi",
                settings={"model_name": "test"},
                prefix_base=_PREFIX,
                enabled_tools_pre_merge={},
                turn_scratch={},
                client=None,
                kv_tracker=_KVCacheTracker(),
                schema_overrides={},
                accumulators={"merged_enabled_tools": {}, "extras": []},
            )
        )

    snapshot = captured["character"]
    assert captured["character_id"] == "c1"
    assert snapshot["personality"] == "warm"
    # _readonly recursively freezes: nested lists arrive as tuples.
    assert snapshot["tags"] == ("a", "b")
    with pytest.raises(TypeError):
        snapshot["personality"] = "x"


async def test_post_pipeline_ctx_carries_readonly_card_snapshot():
    captured = {}

    async def mock_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "draft"}

    async def post_hook(post_ctx):
        captured["character"] = post_ctx.character
        captured["character_id"] = post_ctx.character_id
        yield {"event": "noop", "data": {}}

    w = make_workflow("cf_post", post_pipeline=post_hook)
    with register_for_test(w):
        with patch("backend.orchestrator.writer_pass", new=mock_writer):
            await _drain(
                _run_pipeline(
                    LLMClient("http://localhost:9999"),
                    _SETTINGS,
                    _DIRECTOR_STATE,
                    [],
                    [],
                    "hello",
                    character_id="c1",
                    card=dict(_CARD),
                    **_pipeline_kwargs(),
                )
            )

    snapshot = captured["character"]
    assert captured["character_id"] == "c1"
    assert snapshot["name"] == "Aria"
    with pytest.raises(TypeError):
        snapshot["name"] = "x"


async def test_on_demand_ctx_carries_card_snapshot_from_route(client):
    await create_character_card({"id": "card_od", "name": "Odette", "description": "d", "personality": "sly"})
    await create_conversation("conv_od", "T", "Odette", "", character_card_id="card_od")
    captured = {}

    async def on_demand(ctx, _body):
        captured["character"] = ctx.character
        captured["character_id"] = ctx.character_id
        return {"ok": True}

    wf = make_workflow("cf_od", on_demand=on_demand)
    with register_for_test(wf):
        resp = await client.post("/api/conversations/conv_od/workflows/cf_od/trigger", json={})

    assert resp.status_code == 200
    assert captured["character_id"] == "card_od"
    assert captured["character"]["personality"] == "sly"
    with pytest.raises(TypeError):
        captured["character"]["name"] = "x"


async def test_on_demand_ctx_card_none_when_conversation_has_no_card(client):
    await create_conversation("conv_nocard", "T", "X", "")
    captured = {}

    async def on_demand(ctx, _body):
        captured["character"] = ctx.character
        captured["character_id"] = ctx.character_id
        return {}

    wf = make_workflow("cf_od_none", on_demand=on_demand)
    with register_for_test(wf):
        resp = await client.post("/api/conversations/conv_nocard/workflows/cf_od_none/trigger", json={})

    assert resp.status_code == 200
    assert captured["character"] is None
    assert captured["character_id"] is None


async def test_regenerate_ctx_carries_card_snapshot_and_id_from_route(client):
    await create_character_card({"id": "card_rg", "name": "Rhea", "description": "d", "personality": "bold"})
    await create_conversation("conv_rg", "T", "Rhea", "", character_card_id="card_rg")
    mid, _ = await add_message("conv_rg", "assistant", "scene", 0)
    await set_active_leaf("conv_rg", mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"OG",
            "workflow_id": "cf_rg",
            "seed": "S",
            "generation_metadata": {"k": 1},
        },
    )
    captured = {}

    async def regenerate(ctx, _body):
        captured["character"] = ctx.character
        captured["character_id"] = ctx.character_id
        return []

    wf = make_workflow(
        "cf_rg",
        regenerate=regenerate,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/conv_rg/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )

    assert resp.status_code == 200
    assert captured["character_id"] == "card_rg"
    assert captured["character"]["personality"] == "bold"
    with pytest.raises(TypeError):
        captured["character"]["name"] = "x"
