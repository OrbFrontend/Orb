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
from backend.workflows import set_workflow_character_state, set_workflow_config
from backend.workflows.image_gen.config import CONFIG_DEFAULTS
from backend.workflows.image_gen.engine import ImageResult


def _image(**info) -> ImageResult:
    return ImageResult(
        image_bytes=b"\x89PNG\r\n\x1a\nimage",
        mime="image/png",
        backend_info={"source": "external_comfy", "workflow_id": "user_a", **info},
    )


async def _status(client) -> dict:
    response = await client.post("/api/workflows/image_gen/query", json={"action": "status"})
    assert response.status_code == 200
    return response.json()


async def _attachment_from(response) -> dict:
    match = re.search(r'"attachment_id":(\d+)', response.text)
    assert match
    attachment = await get_workflow_attachment_by_id(int(match.group(1)))
    assert attachment is not None
    return attachment


@pytest.mark.asyncio
async def test_manifest_and_status_report_the_active_source_and_its_capabilities(client):
    manifest = (await client.get("/api/workflows")).json()
    entry = next(w for w in manifest if w["id"] == "image_gen")
    assert entry["display_name"] == "Image Generation"

    status = await _status(client)
    assert status["source"] == "external_comfy"
    assert status["capabilities"] == {
        "can_generate": True,
        "can_list_models": True,
        "can_install_curated_models": False,
        "managed_runtime": False,
        # Per-graph in practice, so the static answer is "this backend can express
        # all of them" and the RenderTarget says what one graph will honour --
        # including the size, now that a graph can map `width`/`height` slots.
        "supports_negative_prompt": True,
        "supports_seed": True,
        "supports_dimensions": True,
        "supports_references": True,
    }
    # The source picker, the provider dropdown and the capability line come from one
    # payload, so a backend the router knows about can never be missing here.
    assert {source["id"] for source in status["sources"]} == {"external_comfy", "cloud"}
    assert any(provider["id"] == "xai" for provider in status["providers"])
    # The preset table *projected*: no configured credential may enter this payload.
    assert not any("api_key" in provider for provider in status["providers"])

    styles = (await client.post("/api/workflows/image_gen/query", json={"action": "styles"})).json()["styles"]
    # Structure, not prompt copy-text: the tag strings live in config to be edited.
    assert [s["id"] for s in styles] == ["realistic", "anime"]
    assert styles[0]["prompt"]


@pytest.mark.asyncio
async def test_generate_trigger_streams_terminal_event_and_persists_image(client, monkeypatch):
    await create_character_card({"id": "ig-char", "name": "Iris"})
    await create_conversation("ig-conv", "Images", "Iris", "A moonlit room", character_card_id="ig-char")
    mid, _ = await add_message("ig-conv", "assistant", "Iris sits beside the rain-streaked window.", 0)
    await set_active_leaf("ig-conv", mid)
    await set_workflow_config(
        "image_gen",
        {"source": "external_comfy", "default_style": "anime", "external_comfy": {"api_url": "http://127.0.0.1:8188"}},
    )
    await set_workflow_character_state("ig-char", "image_gen", {"appearance_prompt": "long silver hair"})

    lane: dict = {}
    captured: dict = {}
    resolve_agent_lane = workflow_routes.agent_lane_from_settings

    def capture_agent_lane(settings, *, writer_client=None, abort_token=None):
        resolved = resolve_agent_lane(settings, writer_client=writer_client, abort_token=abort_token)
        lane["writer_client"] = writer_client
        lane["agent_client"], lane["agent_model_name"] = resolved
        return resolved

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return "1girl, long silver hair, sitting, window, rain, night", "day", "single_call"

    async def fake_render(adapter, request, **kwargs):
        captured["request"] = request
        return _image(backend_model="anime.safetensors")

    monkeypatch.setattr(workflow_routes, "agent_lane_from_settings", capture_agent_lane)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    response = await client.post(
        "/api/conversations/ig-conv/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )
    assert response.status_code == 200
    assert "event: image_gen_done" in response.text

    # Count anchor leads and the style follows immediately (see assemble_prompts).
    anime = CONFIG_DEFAULTS["styles"][1]
    assert captured["request"].prompt.startswith(f"1girl, {anime['prompt']}, long silver hair")
    assert captured["request"].prompt.endswith("sitting, window, rain, night")
    assert lane["agent_client"] is lane["writer_client"]
    compose = captured["compose"]
    assert compose["client"] is lane["writer_client"]
    assert compose["model_name"] == lane["agent_model_name"]
    assert compose["prompt_format"] == "hybrid"
    assert compose["profile_owner_name"] == "Iris"
    assert compose["style_prompt"] == anime["prompt"]
    assert compose["style_negative_prompt"] == anime["negative_prompt"]
    assert compose["profile_negative_prompt"] == ""

    attachment = await _attachment_from(response)
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
        {"source": "external_comfy", "default_style": "anime", "external_comfy": {"api_url": "http://127.0.0.1:8188"}},
    )

    async def fake_compose(**kwargs):
        return "1girl, standing", "", "single_call"

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render)
    response = await client.post(
        f"/api/conversations/{conv_id}/workflows/image_gen/trigger", json={"action": "generate", "message_id": mid}
    )
    assert response.status_code == 200
    return response.text


@pytest.mark.asyncio
async def test_generate_stream_relays_queue_position_while_the_render_runs(client, monkeypatch):
    async def render(adapter, request, *, progress=None, **kwargs):
        assert progress is not None, "the hook must pass a progress callback to the adapter"
        progress("queued", {"number": 7, "ahead": 2})
        progress("rendering", {"number": 7, "ahead": 0})
        return _image()

    body = await _stream_generate(client, monkeypatch, render, "ig-queued")
    assert re.findall(r'"label":"([^"]+)"', body) == [
        "Composing image prompt...",
        "Queued behind 2 renders...",
        "Rendering in ComfyUI...",
    ]
    # Every phase label precedes the terminal frame rather than trailing the work.
    assert body.index("Rendering in ComfyUI") < body.index("image_gen_done")


@pytest.mark.asyncio
async def test_render_phase_is_reported_by_the_adapter_not_assumed(client, monkeypatch):
    async def render(adapter, request, *, progress=None, **kwargs):
        return _image()

    body = await _stream_generate(client, monkeypatch, render, "ig-silent")
    # A silent adapter yields no render phase; the label is never synthesized after
    # the fact, which is what made it arrive a full render too late.
    assert re.findall(r'"label":"([^"]+)"', body) == ["Composing image prompt..."]
    assert "event: image_gen_done" in body


@pytest.mark.asyncio
async def test_config_round_trips_through_the_workflow_normalizer(client):
    """`config_schema` is UI metadata and enforces nothing, so the write path has to
    normalize. Otherwise the panel keeps listing a workflow the render path silently
    drops on read -- a setting that appears to have taken effect.

    Which values normalize to what is `test_config`'s subject; this is only that the
    route runs the normalizer at all, on both ends."""
    oversized = {
        "id": "user_big",
        "label": "Too big",
        "graph": {str(i): {"class_type": "CLIPTextEncode", "inputs": {"text": "x" * 200}} for i in range(4_000)},
        "slots": {"positive": ["0", "text"], "seed": ["0", "text"], "output": ["0", "text"]},
    }
    response = await client.put(
        "/api/workflows/image_gen/config",
        json={"config": {"external_comfy": {"api_url": "http://comfy.test:8188", "user_graphs": [oversized]}}},
    )

    assert response.status_code == 200
    stored = response.json()["config"]
    assert stored["external_comfy"]["user_graphs"] == []
    # And the read path agrees, so reopening settings shows what will be used.
    assert (await client.get("/api/workflows/image_gen/config")).json()["config"] == stored


_OVERRIDE_GRAPH = {
    "id": "user_override",
    "label": "Override",
    "graph": {
        "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "m": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
        "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
    },
    # A model-override graph: the checkpoint slot marks the input Orb replaces.
    "slots": {
        "positive": ["0", "text"],
        "seed": ["s", "seed"],
        "output": ["o", "images"],
        "checkpoint": ["m", "ckpt_name"],
    },
}
# The same graph carrying its own loaders, which needs no checkpoint from Orb.
_SELF_CONTAINED = {
    **_OVERRIDE_GRAPH,
    "id": "user_self",
    "label": "Self",
    "slots": {k: v for k, v in _OVERRIDE_GRAPH["slots"].items() if k != "checkpoint"},
}


@pytest.mark.asyncio
async def test_status_reports_why_the_style_that_will_render_cannot(client):
    """Readiness follows the *default* style, not the whole list. Auditing every
    style was right while ComfyUI was the only backend; it now reads as a
    permanently stuck "Setup required" the moment a cloud style exists, since a
    cloud style has no workflow and never will."""
    styles = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    graphs = [_OVERRIDE_GRAPH, _SELF_CONTAINED]

    async def store(**external) -> dict:
        await set_workflow_config("image_gen", {"external_comfy": {"styles": styles, **external}})
        return await _status(client)

    # External mode ships no default graph, so nothing pinned is reported first.
    assert (await store())["reason"] == "no_workflow"

    # Pinned, but it overrides the model and no checkpoint is chosen.
    for s in styles:
        s["workflow"] = "user_override"
    assert (await store(user_graphs=graphs))["reason"] == "no_checkpoint"

    # One style on the self-contained graph, one with a checkpoint: both can render.
    styles[0]["workflow"] = "user_self"
    styles[1]["checkpoint"] = "anime.safetensors"
    ready = await store(user_graphs=graphs)
    assert ready["ready"] is True
    assert ready["style_count"] == 2

    # A half-finished style, or one rendering somewhere else entirely, does not take
    # the whole card offline...
    styles.append({"id": "cloud_style", "label": "Grok", "connection": "xai"})
    styles.append({"id": "half_done", "label": "New style"})
    still_ready = await store(user_graphs=graphs)
    assert still_ready["ready"] is True
    assert still_ready["source"] == "external_comfy"

    # ...but pointing the config at the unfinished one names that style, rather than
    # a generic "assign one to each".
    await set_workflow_config(
        "image_gen", {"default_style": "half_done", "external_comfy": {"styles": styles, "user_graphs": graphs}}
    )
    unfinished = await _status(client)
    assert unfinished["reason"] == "no_workflow"
    assert "New style" in unfinished["detail"]


@pytest.mark.asyncio
async def test_a_cloud_style_is_never_asked_for_a_comfyui_workflow(client):
    """Workflows are a ComfyUI concept. Selecting a style that renders on a cloud
    connection routes readiness to that connection's adapter, which knows nothing
    about graphs -- the two backends' prerequisites never mix. There are no
    user_graphs here at all, which under the ComfyUI adapter is `no_workflow`."""

    async def store(api_key: str) -> dict:
        await set_workflow_config(
            "image_gen",
            {
                "default_style": "grok",
                "styles": [{"id": "grok", "label": "Grok", "connection": "xai"}],
                "cloud": {"providers": {"xai": {"api_key": api_key, "model": "grok-imagine-image"}}},
            },
        )
        return await _status(client)

    ready = await store("k")
    assert ready["source"] == "cloud"
    assert ready["ready"] is True
    # The cloud adapter's own prerequisites still apply, and they are its own.
    assert (await store(""))["reason"] == "no_api_key"


@pytest.mark.asyncio
async def test_a_reference_image_the_normalizer_drops_is_reported_not_swallowed(client):
    """`normalize_profile` drops an image it cannot accept rather than truncating it
    -- half a base64 payload is not a smaller image. Answering a bare "ok" let the
    settings form preview the picture, report a successful save, and show it gone on
    the next open with nothing to explain why."""
    await create_character_card({"id": "ig-drop", "name": "Iris"})
    await create_conversation("ig-drop-conv", "Images", "Iris", "A room", character_card_id="ig-drop")

    async def save(profile: dict) -> dict:
        response = await client.post(
            "/api/conversations/ig-drop-conv/workflows/image_gen/trigger",
            json={"action": "set_profile", "profile": profile},
        )
        assert response.status_code == 200
        return response.json()

    # A GIF is `image/*` and is not one of the three mimes Orb declares.
    rejected = await save(
        {"appearance_prompt": "silver hair", "reference_image_b64": "Ym9ndXM=", "reference_mime": "image/gif"}
    )
    assert rejected["profile"]["reference_image_b64"] == ""
    assert "not saved" in rejected["warning"]
    # The rest of the profile still saved, so the warning is about the image alone.
    assert rejected["profile"]["appearance_prompt"] == "silver hair"

    accepted = await save({"reference_image_b64": "Ym9ndXM=", "reference_mime": "image/png"})
    assert accepted["profile"]["reference_image_b64"] == "Ym9ndXM="
    assert "warning" not in accepted
    # And a save that carries no image at all is not accused of losing one.
    assert "warning" not in await save({"appearance_prompt": "silver hair"})


@pytest.mark.asyncio
async def test_a_cloud_reference_that_resolves_to_nothing_renders_anyway_and_says_so(client, monkeypatch):
    """A ComfyUI graph built around a `LoadImage` cannot render without one. A cloud
    provider can -- the same model has a plain generations endpoint one field away --
    so refusing would make turning reference images on break every new conversation
    until an image exists in it."""
    await create_conversation("ig-optional", "Images", "Iris", "A quiet room")
    mid, _ = await add_message("ig-optional", "assistant", "She turns toward the door.", 0)
    await set_active_leaf("ig-optional", mid)
    await set_workflow_config(
        "image_gen",
        {
            "default_style": "grok",
            "styles": [{"id": "grok", "label": "Grok", "connection": "xai"}],
            "cloud": {"providers": {"xai": {"api_key": "k", "model": "grok-imagine-image", "reference_source": "previous"}}},
        },
    )
    captured: dict = {}

    async def fake_compose(**kwargs):
        captured["compose"] = kwargs
        return "1girl, standing", "", "single_call"

    async def fake_render(adapter, request, **kwargs):
        captured["request"] = request
        return _image(source="cloud")

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    response = await client.post(
        "/api/conversations/ig-optional/workflows/image_gen/trigger", json={"action": "generate", "message_id": mid}
    )
    assert response.status_code == 200
    assert "event: image_gen_error" not in response.text

    assert captured["request"].references == ()
    # The composer is told there is no reference, so it still describes identity.
    assert captured["compose"]["has_references"] is False
    attachment = await _attachment_from(response)
    notes = json.loads(attachment["consumption_metadata"])["notes"]
    assert any("no reference image was available" in note for note in notes)


@pytest.mark.asyncio
async def test_the_query_surface_reports_its_own_failures_in_band(client):
    """A bad request shape or an unknown action answers 200 + `{"error"}` like the
    rest of the query surface, so the caller degrades rather than treating it as a
    transport failure. Only a missing QUERY *binding* is a route-level 404."""
    bad_shape = await client.post("/api/workflows/image_gen/query", json={"action": "node_types", "class_types": "KSampler"})
    assert bad_shape.status_code == 200
    assert "class_types" in bad_shape.json()["error"]

    unknown = await client.post("/api/workflows/image_gen/query", json={"action": "does_not_exist"})
    assert unknown.status_code == 200
    assert "unknown action" in unknown.json()["error"]

    # An unregistered workflow and a registered one with no QUERY binding are
    # indistinguishable, and both 404 at the route before any hook.
    assert (await client.post("/api/workflows/nope/query", json={"action": "status"})).status_code == 404
    assert (await client.post("/api/workflows/format_consistency/query", json={"action": "status"})).status_code == 404


@pytest.mark.asyncio
async def test_completing_a_turn_produces_no_image_and_no_image_inference(client, monkeypatch):
    """The on-demand-only contract, asserted directly rather than inferred from the
    absence of a POST_PIPELINE binding -- a future binding added by accident is
    exactly what this is here to catch."""

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

    assert not [att for att in events[-1].staged_attachments if att.get("workflow_id") == "image_gen"]
    assert await get_workflow_attachments_for_message(mid) == []
