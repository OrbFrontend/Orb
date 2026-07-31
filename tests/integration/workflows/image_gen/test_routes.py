from __future__ import annotations

import json
import re

import pytest

import backend.api.routes.workflows as workflow_routes
from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    get_workflow_attachment_by_id,
    get_workflow_attachments_for_message,
    set_active_leaf,
)
from backend.inference import LLMClient, _KVCacheTracker
from backend.pipeline.workflow_bridge import _run_post_pipeline
from backend.workflows import (
    HookType,
    get_workflow,
    set_workflow_character_state,
    set_workflow_config,
    workflow_has_hook,
)
from backend.workflows.image_gen.config import CONFIG_DEFAULTS
from backend.workflows.image_gen.engine import ImageResult


@pytest.mark.asyncio
async def test_manifest_and_status_expose_external_only_stage(client):
    manifest = (await client.get("/api/workflows")).json()
    entry = next(w for w in manifest if w["id"] == "image_gen")
    assert entry["display_name"] == "Image Generation"

    status = (await client.post("/api/workflows/image_gen/query", json={"action": "status"})).json()
    assert status["source"] == "external_comfy"
    assert status["capabilities"] == {
        "can_generate": True,
        "can_list_models": True,
        "can_install_curated_models": False,
        "managed_runtime": False,
    }

    styles = (await client.post("/api/workflows/image_gen/query", json={"action": "styles"})).json()["styles"]
    # Structure, not prompt copy-text: the tag strings live in config and are meant to be edited.
    assert [s["id"] for s in styles] == ["realistic", "anime"]
    assert styles[0]["prompt"] and "prompt_default" not in styles[0]


def test_image_generation_is_on_demand_only():
    workflow = get_workflow("image_gen")
    assert workflow is not None
    assert workflow_has_hook(workflow, HookType.ON_DEMAND)
    # QUERY is off-turn too (global config/discovery); still no in-turn binding.
    assert workflow_has_hook(workflow, HookType.QUERY)
    assert not workflow_has_hook(workflow, HookType.POST_PIPELINE)
    assert not workflow_has_hook(workflow, HookType.PRE_PIPELINE)


@pytest.mark.asyncio
async def test_generate_trigger_streams_terminal_event_and_persists_image(client, monkeypatch):
    await create_character_card({"id": "ig-char", "name": "Iris"})
    await create_conversation("ig-conv", "Images", "Iris", "A moonlit room", character_card_id="ig-char")
    mid, _ = await add_message("ig-conv", "assistant", "Iris sits beside the rain-streaked window.", 0)
    await set_active_leaf("ig-conv", mid)
    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    await set_workflow_character_state("ig-char", "image_gen", {"appearance_prompt": "long silver hair"})

    lane = {}
    resolve_agent_lane = workflow_routes.agent_lane_from_settings

    def capture_agent_lane(settings, *, writer_client=None, abort_token=None):
        resolved = resolve_agent_lane(settings, writer_client=writer_client, abort_token=abort_token)
        lane["writer_client"] = writer_client
        lane["agent_client"], lane["agent_model_name"] = resolved
        return resolved

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return "1girl, long silver hair, sitting, window, rain, night", "day", "single_call"

    captured = {}

    async def fake_render(config, request, **kwargs):
        captured["request"] = request
        return ImageResult(
            image_bytes=b"\x89PNG\r\n\x1a\nimage",
            mime="image/png",
            backend_info={
                "source": "external_comfy",
                "workflow_id": "user_a",
                "backend_model": "anime.safetensors",
            },
        )

    monkeypatch.setattr(workflow_routes, "agent_lane_from_settings", capture_agent_lane)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    response = await client.post(
        "/api/conversations/ig-conv/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )
    assert response.status_code == 200
    assert "event: image_gen_done" in response.text
    assert '"attachment_id":' in response.text
    # Count anchor leads and the style follows immediately (see assemble_prompts).
    anime_prompt = CONFIG_DEFAULTS["external_comfy"]["styles"][1]["prompt"]
    assert captured["request"].prompt.startswith(f"1girl, {anime_prompt}, long silver hair")
    assert captured["request"].prompt.endswith("sitting, window, rain, night")
    assert lane["agent_client"] is lane["writer_client"]
    assert captured["compose"]["client"] is lane["writer_client"]
    assert captured["compose"]["model_name"] == lane["agent_model_name"]
    assert captured["compose"]["prompt_format"] == "hybrid"
    assert captured["compose"]["profile_owner_name"] == "Iris"
    assert captured["compose"]["style_prompt"] == anime_prompt
    assert captured["compose"]["style_negative_prompt"] == CONFIG_DEFAULTS["external_comfy"]["styles"][1]["negative_prompt"]
    assert captured["compose"]["profile_negative_prompt"] == ""

    match = re.search(r'"attachment_id":(\d+)', response.text)
    assert match
    attachment = await get_workflow_attachment_by_id(int(match.group(1)))
    assert attachment is not None
    assert attachment["mime_type"] == "image/png"
    assert attachment["seed"]
    assert json.loads(attachment["generation_metadata"])["backend_model"] == "anime.safetensors"


async def _stream_generate(client, monkeypatch, render, conv_id):
    """Drive one generate stream against a stubbed composer and renderer."""
    await create_conversation(conv_id, "Phases", "Iris", "A quiet room")
    mid, _ = await add_message(conv_id, "assistant", "She turns toward the door.", 0)
    await set_active_leaf(conv_id, mid)
    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "external_comfy": {"api_url": "http://127.0.0.1:8188", "checkpoint": "a.safetensors"},
        },
    )

    async def fake_compose(**kwargs):
        return "1girl, standing", "", "single_call"

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render)
    response = await client.post(
        f"/api/conversations/{conv_id}/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid},
    )
    assert response.status_code == 200
    return response.text


def _image() -> ImageResult:
    return ImageResult(
        image_bytes=b"\x89PNG\r\n\x1a\nimage",
        mime="image/png",
        backend_info={"source": "external_comfy", "workflow_id": "user_a"},
    )


@pytest.mark.asyncio
async def test_generate_stream_relays_queue_position_while_the_render_runs(client, monkeypatch):
    async def render(config, request, *, progress=None, **kwargs):
        assert progress is not None, "the hook must pass a progress callback to the adapter"
        progress("queued", {"number": 7, "ahead": 2})
        progress("rendering", {"number": 7, "ahead": 0})
        return _image()

    body = await _stream_generate(client, monkeypatch, render, "ig-queued")
    labels = re.findall(r'"label":"([^"]+)"', body)
    assert labels == ["Composing image prompt...", "Queued behind 2 renders...", "Rendering in ComfyUI..."]
    # Every phase label precedes the terminal frame rather than trailing the work.
    assert body.index("Rendering in ComfyUI") < body.index("image_gen_done")


@pytest.mark.asyncio
async def test_render_phase_is_reported_by_the_adapter_not_assumed(client, monkeypatch):
    async def render(config, request, *, progress=None, **kwargs):
        return _image()

    body = await _stream_generate(client, monkeypatch, render, "ig-silent")
    # A silent adapter yields no render phase; the label is never synthesized
    # after the fact, which is what made it arrive a full render too late.
    assert re.findall(r'"label":"([^"]+)"', body) == ["Composing image prompt..."]
    assert "event: image_gen_done" in body


@pytest.mark.asyncio
async def test_config_round_trips_through_the_workflow_normalizer(client):
    """`config_schema` is UI metadata and enforces nothing, so the write path has
    to normalize. Otherwise the panel keeps listing a workflow the render path
    silently drops on read -- a setting that appears to have taken effect."""
    oversized = {
        "id": "user_big",
        "label": "Too big",
        "graph": {str(i): {"class_type": "CLIPTextEncode", "inputs": {"text": "x" * 200}} for i in range(4_000)},
        "slots": {"positive": ["0", "text"], "seed": ["0", "text"], "output": ["0", "text"]},
    }
    response = await client.put(
        "/api/workflows/image_gen/config",
        json={
            "config": {
                "timeout_seconds": "99999",
                "default_style": "does-not-exist",
                "prompter_reasoning": True,
                "external_comfy": {
                    "api_url": "http://user:secret@comfy.test:8188/",
                    "user_graphs": [oversized],
                    "unknown_key": "dropped",
                },
            }
        },
    )

    assert response.status_code == 200
    stored = response.json()["config"]
    assert stored["timeout_seconds"] == 900.0
    assert stored["default_style"] == "realistic"
    assert stored["prompter_reasoning"] is True
    assert stored["external_comfy"]["api_url"] == "http://127.0.0.1:8188"
    assert stored["external_comfy"]["user_graphs"] == []
    assert "unknown_key" not in stored["external_comfy"]
    # And the read path agrees, so reopening settings shows what will be used.
    assert (await client.get("/api/workflows/image_gen/config")).json()["config"] == stored


@pytest.mark.asyncio
async def test_status_reports_why_it_is_not_ready(client):
    async def status() -> dict:
        return (await client.post("/api/workflows/image_gen/query", json={"action": "status"})).json()

    # A model-override graph (checkpoint slot) and a self-contained one (none).
    override_graph = {
        "id": "user_override",
        "label": "Override",
        "graph": {
            "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
            "m": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
            "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
        },
        "slots": {
            "positive": ["0", "text"],
            "seed": ["s", "seed"],
            "output": ["o", "images"],
            "checkpoint": ["m", "ckpt_name"],
        },
    }
    self_contained = {**override_graph, "id": "user_self", "label": "Self"}
    self_contained["slots"] = {k: v for k, v in override_graph["slots"].items() if k != "checkpoint"}

    # No workflow assigned: external mode ships no default graph, so the styles
    # cannot render until one is imported and pinned -- reported first.
    styles = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    await set_workflow_config("image_gen", {"external_comfy": {"styles": styles}})
    first = await status()
    assert first["ready"] is False
    assert first["reason"] == "no_workflow"

    # Workflow assigned but it overrides the model and no checkpoint is chosen.
    for s in styles:
        s["workflow"] = "user_override"
    await set_workflow_config(
        "image_gen", {"external_comfy": {"styles": styles, "user_graphs": [override_graph, self_contained]}}
    )
    second = await status()
    assert second["ready"] is False
    assert second["reason"] == "no_checkpoint"

    # Point one style at the self-contained graph (needs no checkpoint) and give
    # the override style a checkpoint: now everything can render.
    styles[0]["workflow"] = "user_self"
    styles[1]["checkpoint"] = "anime.safetensors"
    await set_workflow_config(
        "image_gen", {"external_comfy": {"styles": styles, "user_graphs": [override_graph, self_contained]}}
    )
    third = await status()
    assert third["ready"] is True
    assert third["style_count"] == 2


@pytest.mark.asyncio
async def test_node_types_query_rejects_a_non_list_in_band(client):
    """A bad request shape is reported in-band (200 + {"error"}) like the rest of
    the query surface, not as an HTTP 4xx -- the caller degrades to conventional
    input names rather than treating it as a transport failure."""
    response = await client.post("/api/workflows/image_gen/query", json={"action": "node_types", "class_types": "KSampler"})
    assert response.status_code == 200
    assert "class_types" in response.json()["error"]


@pytest.mark.asyncio
async def test_query_route_rejects_unknown_workflow_and_unknown_action(client):
    # Unregistered workflow: 404 at the route, before any hook.
    assert (await client.post("/api/workflows/nope/query", json={"action": "status"})).status_code == 404
    # A registered workflow with no QUERY binding is indistinguishable: 404.
    # (format_consistency binds only POST_PIPELINE; tts and image_gen both have
    # QUERY now, so neither is a no-binding case any longer.)
    assert (await client.post("/api/workflows/format_consistency/query", json={"action": "status"})).status_code == 404
    # A registered QUERY handler that does not recognize the action answers
    # in-band rather than raising, so the route stays 200.
    unknown = await client.post("/api/workflows/image_gen/query", json={"action": "does_not_exist"})
    assert unknown.status_code == 200
    assert "unknown action" in unknown.json()["error"]


@pytest.mark.asyncio
async def test_completing_a_turn_produces_no_image_and_no_image_inference(client, monkeypatch):
    """The on-demand-only contract, asserted directly rather than inferred from
    the absence of a POST_PIPELINE binding -- a future binding added by accident
    is exactly what this is here to catch."""

    async def forbidden(*args, **kwargs):
        raise AssertionError("image generation ran inside a turn")

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", forbidden)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", forbidden)

    await create_conversation("ig-turn", "Turn", "Iris", "A quiet room")
    mid, _ = await add_message("ig-turn", "assistant", "She turns toward the door.", 0)
    await set_active_leaf("ig-turn", mid)

    events = [
        event
        async for event in _run_post_pipeline(
            draft="She turns toward the door.",
            conversation_id="ig-turn",
            character_id=None,
            card=None,
            history=[],
            effective_msg="draw her",
            director_output={},
            settings={"model_name": "test", "enabled_tools": {}, "reasoning_enabled_passes": {}},
            prefix=[{"role": "system", "content": "You are an assistant."}],
            enabled_tools={},
            turn_scratch={},
            client=LLMClient("http://localhost:9999"),
            kv_tracker=_KVCacheTracker(),
            schema_overrides={},
        )
    ]

    result = events[-1]
    assert not [att for att in result.staged_attachments if att.get("workflow_id") == "image_gen"]
    assert await get_workflow_attachments_for_message(mid) == []
