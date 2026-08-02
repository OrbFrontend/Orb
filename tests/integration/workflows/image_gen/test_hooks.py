"""On-demand guards, replay routing, and the on-demand-only contract.

The generate action returns a `StreamingResponse` whose body is consumed after
the trigger route has released its locks, so every one of these asserts against
the *stream* rather than a JSON body -- a guard that answers with plain JSON
here is invisible to the client, which is parsing SSE frames.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    get_workflow_attachments_for_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows import set_workflow_character_state, set_workflow_config
from backend.workflows.image_gen import pov
from backend.workflows.image_gen.engine import ImageGenerationError, ImageResult

# Reroll/rehydrate route through the adapter, so the render seam is its `generate`.
_COMFY_GENERATE = "backend.workflows.image_gen.engine.adapters.external_comfy.ExternalComfyAdapter.generate"

USER_GRAPH = {
    "id": "user_a",
    "label": "Mine",
    "graph": {
        "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
    },
    "slots": {"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]},
}

CONFIG = {
    "source": "external_comfy",
    "default_style": "anime",
    "external_comfy": {
        "api_url": "http://127.0.0.1:8188",
        "user_graphs": [USER_GRAPH],
    },
}


def _image(**info) -> ImageResult:
    return ImageResult(
        image_bytes=b"\x89PNG\r\n\x1a\nimage",
        mime="image/png",
        backend_info={"source": "external_comfy", "workflow_id": "user_a", **info},
    )


def _events(body: str) -> list[tuple[str, dict]]:
    frames = []
    for frame in body.split("\n\n"):
        if not frame.strip():
            continue
        name = data = None
        for line in frame.split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            frames.append((name, json.loads(data) if data else {}))
    return frames


async def _seed(conv_id: str, *, with_character: bool = False) -> int:
    if with_character:
        await create_character_card({"id": f"{conv_id}-char", "name": "Iris"})
    await create_conversation(
        conv_id,
        "Images",
        "Iris",
        "A quiet room",
        character_card_id=f"{conv_id}-char" if with_character else None,
    )
    mid, _ = await add_message(conv_id, "assistant", "She turns toward the door.", 0)
    await set_active_leaf(conv_id, mid)
    await set_workflow_config("image_gen", CONFIG)
    return mid


async def _trigger(client, conv_id: str, body: dict) -> list[tuple[str, dict]]:
    response = await client.post(f"/api/conversations/{conv_id}/workflows/image_gen/trigger", json=body)
    assert response.status_code == 200
    return _events(response.text)


def _stub(monkeypatch, render=None, scene="1girl, standing"):
    async def fake_compose(**kwargs):
        return scene, "", "single_call"

    async def fake_render(config, request, **kwargs):
        return _image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render or fake_render)


# ── guards ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_key, message",
    [("bad_type", "message_id (int) required"), ("missing", "no longer part of this conversation")],
)
async def test_a_rejected_generate_still_speaks_the_stream_contract(client, monkeypatch, body_key, message):
    """A guard that answers in JSON leaves the client waiting on frames that
    never come: it sees no terminal event and re-enables its button silently."""
    conv_id = f"ig-guard-{body_key}"
    await _seed(conv_id)
    _stub(monkeypatch)
    body = {"action": "generate", "message_id": True if body_key == "bad_type" else 999_999}

    events = await _trigger(client, conv_id, body)

    assert [name for name, _ in events] == ["image_gen_error", "phase_status", "image_gen_done"]
    assert message in events[0][1]["message"]
    assert events[-1][1] == {"attachment_id": None}


@pytest.mark.asyncio
async def test_generate_refuses_a_user_message(client, monkeypatch):
    await _seed("ig-user-msg")
    uid, _ = await add_message("ig-user-msg", "user", "draw her", 1)
    _stub(monkeypatch)

    events = await _trigger(client, "ig-user-msg", {"action": "generate", "message_id": uid})

    assert "assistant messages" in events[0][1]["message"]


@pytest.mark.asyncio
async def test_generate_refuses_a_message_off_the_active_branch(client, monkeypatch):
    """Conversation membership is not branch membership: composing from a
    history that never reaches the anchor describes replies that came after it."""
    mid = await _seed("ig-branch")
    other, _ = await add_message("ig-branch", "assistant", "a different branch", 0)
    _stub(monkeypatch)
    assert other != mid

    events = await _trigger(client, "ig-branch", {"action": "generate", "message_id": other})

    assert "active branch" in events[0][1]["message"]
    assert events[-1][1] == {"attachment_id": None}


@pytest.mark.asyncio
async def test_a_render_failure_reaches_the_user_and_still_terminates(client, monkeypatch):
    mid = await _seed("ig-fail")

    async def boom(config, request, **kwargs):
        raise ValueError("ComfyUI could not complete the image")

    _stub(monkeypatch, render=boom)

    events = await _trigger(client, "ig-fail", {"action": "generate", "message_id": mid})
    names = [name for name, _ in events]

    assert names[-1] == "image_gen_done"
    assert events[-1][1] == {"attachment_id": None}
    assert any(name == "image_gen_error" and "could not complete" in data["message"] for name, data in events)


# ── concurrency ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_concurrent_triggers_on_one_message_stay_separate_roots(client, monkeypatch):
    """The stream body runs after the trigger route drops its locks, so nothing
    serializes these two. What must hold is that each lands as its own flat
    attachment root rather than interleaving into a corrupt sibling tree."""
    mid = await _seed("ig-race")
    started = asyncio.Event()

    async def slow_render(config, request, **kwargs):
        started.set()
        await asyncio.sleep(0.05)
        return _image()

    _stub(monkeypatch, render=slow_render)
    body = {"action": "generate", "message_id": mid}

    first, second = await asyncio.gather(
        _trigger(client, "ig-race", body),
        _trigger(client, "ig-race", body),
    )

    ids = [events[-1][1]["attachment_id"] for events in (first, second)]
    assert all(isinstance(i, int) for i in ids), ids
    assert len(set(ids)) == 2
    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 2
    assert all(row["parent_attachment_id"] is None for row in rows)


# ── replay ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reroll_replays_the_stored_graph_and_model_not_todays_style(client, monkeypatch):
    """Resolving replay through the style would re-render an old attachment on
    whatever checkpoint that style points at now."""
    mid = await _seed("ig-reroll")
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"\x89PNG\r\n\x1a\nold",
            "workflow_id": "image_gen",
            "seed": "1234",
            "generation_metadata": {
                "style_id": "anime",
                "prompt": "1girl, standing",
                "negative_prompt": "",
                "workflow_id": "user_a",
                "backend_model": "original.safetensors",
            },
        },
    )
    captured = {}

    async def capture(_self, request, *, target, progress=None):
        captured.update(checkpoint=target.model, graph_id=target.target_id, seed=request.seed)
        return _image()

    monkeypatch.setattr(_COMFY_GENERATE, capture)

    response = await client.post(
        f"/api/conversations/ig-reroll/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
        json={},
    )

    assert response.status_code == 200
    assert captured["checkpoint"] == "original.safetensors"
    assert captured["graph_id"] == "user_a"
    assert captured["seed"] != 1234, "a reroll must move the seed or it silently returns the cached image"


@pytest.mark.asyncio
async def test_replaying_a_deleted_user_graph_discloses_the_substitution(client, monkeypatch):
    mid = await _seed("ig-gone")
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"\x89PNG\r\n\x1a\nold",
            "workflow_id": "image_gen",
            "seed": "1234",
            "generation_metadata": {
                "style_id": "anime",
                "prompt": "1girl",
                "negative_prompt": "",
                "workflow_id": "user_deleted",
                "backend_model": None,
            },
        },
    )

    async def capture(_self, request, *, target, progress=None):
        return _image(notes=list(target.notes))

    monkeypatch.setattr(_COMFY_GENERATE, capture)

    response = await client.post(
        f"/api/conversations/ig-gone/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
        json={},
    )
    assert response.status_code == 200
    rows = await get_workflow_attachments_for_message(mid)
    sibling = next(row for row in rows if row["id"] != aid)
    notes = json.loads(sibling["consumption_metadata"])["notes"]
    assert any("user_deleted" in note for note in notes)


async def _seed_pinned(conv_id: str) -> int:
    """An artifact whose stored params pin the graph/checkpoint its style resolved to."""
    mid = await _seed(conv_id)
    return mid, await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"\x89PNG\r\n\x1a\nold",
            "workflow_id": "image_gen",
            "seed": "1234",
            "generation_metadata": {
                "style_id": "anime",
                "prompt": "1girl",
                "negative_prompt": "",
                "workflow_id": "user_a",
                "backend_model": "original.safetensors",
            },
            "consumption_metadata": {"style_id": "anime", "style_label": "Anime"},
        },
    )


@pytest.mark.asyncio
async def test_a_style_override_drops_the_old_style_pins_and_discloses_the_wording(client, monkeypatch):
    """Swapping style retargets the render; the pins describe the OLD style, so they go.
    The prompt text cannot follow -- only the assembled string is stored -- so say so."""
    mid, aid = await _seed_pinned("ig-swap")
    captured = {}

    async def capture(_self, request, *, target, progress=None):
        captured.update(checkpoint=target.model, graph_id=target.target_id)
        return _image(notes=list(target.notes))

    monkeypatch.setattr(_COMFY_GENERATE, capture)

    response = await client.post(
        f"/api/conversations/ig-swap/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
        json={"params": {"style_id": "realistic"}},
    )

    assert response.status_code == 200
    assert captured["graph_id"] == ""  # the realistic style pins neither
    assert captured["checkpoint"] == ""
    rows = await get_workflow_attachments_for_message(mid)
    sibling = next(row for row in rows if row["id"] != aid)
    stored = json.loads(sibling["generation_metadata"])
    assert stored["style_id"] == "realistic"
    # Dropped, not blanked: the sibling carries no pins at all, so its own future
    # rerolls resolve through the style it now names.
    assert "workflow_id" not in stored and "backend_model" not in stored
    cm = json.loads(sibling["consumption_metadata"])
    assert cm["style_id"] == "realistic"
    assert any("still carries the previous style" in note for note in cm["notes"])


@pytest.mark.asyncio
async def test_an_override_that_keeps_the_style_keeps_the_pins(client, monkeypatch):
    mid, aid = await _seed_pinned("ig-noswap")
    captured = {}

    async def capture(_self, request, *, target, progress=None):
        captured.update(checkpoint=target.model, graph_id=target.target_id, prompt=request.prompt)
        return _image()

    monkeypatch.setattr(_COMFY_GENERATE, capture)

    response = await client.post(
        f"/api/conversations/ig-noswap/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
        json={"params": {"style_id": "anime", "prompt": "edited"}},
    )

    assert response.status_code == 200
    assert captured["graph_id"] == "user_a"
    assert captured["checkpoint"] == "original.safetensors"
    assert captured["prompt"] == "edited"
    rows = await get_workflow_attachments_for_message(mid)
    sibling = next(row for row in rows if row["id"] != aid)
    assert "notes" not in json.loads(sibling["consumption_metadata"])


@pytest.mark.asyncio
async def test_regenerate_recomposes_under_the_current_style_as_a_sibling(client, monkeypatch):
    mid = await _seed("ig-regen")
    agent_endpoint = await client.post("/api/endpoints", json={"url": "http://regen-agent.local", "api_key": "agent-key"})
    assert agent_endpoint.status_code == 200
    agent_model = await client.put(
        f"/api/models/{agent_endpoint.json()['agent_active_model_config_id']}",
        json={"model_name": "regen-agent-model"},
    )
    assert agent_model.status_code == 200
    settings = await client.put(
        "/api/settings",
        json={
            "agent_same_as_writer": False,
            "agent_endpoint_id": agent_endpoint.json()["id"],
            "agent_shared_system_prompt": "Regeneration agent system.",
        },
    )
    assert settings.status_code == 200

    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"\x89PNG\r\n\x1a\nold",
            "workflow_id": "image_gen",
            "seed": "1234",
            "generation_metadata": {"style_id": "realistic", "prompt": "stale", "negative_prompt": ""},
        },
    )
    captured = {}

    async def fake_compose(**kwargs):
        captured.update(kwargs)
        return "1girl, doorway, looking back", "", "single_call"

    async def fake_render(config, request, **kwargs):
        return _image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    response = await client.post(
        f"/api/conversations/ig-regen/messages/{mid}/workflow-attachments/{aid}/regenerate",
        json={},
    )

    assert response.status_code == 200
    rows = await get_workflow_attachments_for_message(mid)
    sibling = next(row for row in rows if row["id"] != aid)
    assert sibling["parent_attachment_id"] == aid
    metadata = json.loads(sibling["generation_metadata"])
    # Recomposed from current settings, not replayed from the predecessor: both
    # its stored prompt and its realistic style are left behind.
    assert metadata["style_id"] == "anime"
    assert "doorway" in metadata["prompt"]
    assert "anime illustration" in metadata["prompt"]
    assert captured["client"].base_url == "http://regen-agent.local"
    assert captured["model_name"] == "regen-agent-model"
    assert captured["prefix"][0]["content"].startswith("Regeneration agent system.")
    assert captured["reasoning_on"] is False


# ── camera ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pov_mode_is_global_config(client):
    """The camera rides the workflow config, like the style: one setting for every
    conversation, saved through the same config PUT the style picker uses."""
    await _seed("ig-pov-a")
    await client.put("/api/workflows/image_gen/config", json={"config": {**CONFIG, "pov_mode": "first"}})

    stored = (await client.get("/api/workflows/image_gen/config")).json()["config"]
    assert stored["pov_mode"] == "first"

    # Junk normalizes rather than persisting an unrenderable mode.
    await client.put("/api/workflows/image_gen/config", json={"config": {**CONFIG, "pov_mode": "sideways"}})
    assert (await client.get("/api/workflows/image_gen/config")).json()["config"]["pov_mode"] == "auto"

    # The picker labels "Auto" off these two, so both must be answered whatever
    # the machine has on disk -- a dev box with the GGUF present reports ready,
    # and an absent flag would leave the label lying either way.
    status = (await client.post("/api/workflows/image_gen/query", json={"action": "status"})).json()
    assert isinstance(status["classifier_ready"], bool)
    assert status["fallback_mode"] == pov.DEFAULT_POV_MODE


@pytest.mark.asyncio
async def test_generate_records_the_camera_and_the_lever_that_chose_it(client, monkeypatch):
    mid = await _seed("ig-pov-meta", with_character=True)
    await set_workflow_character_state("ig-pov-meta-char", "image_gen", {"appearance_prompt": "3D, third_person"})
    seen: dict = {}

    async def fake_compose(**kwargs):
        seen.update(kwargs)
        return "1girl, standing", "", "single_call"

    async def fake_render(config, request, **kwargs):
        return _image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    # The picker decides. A 'third_person' tag left in the character's appearance
    # prompt is appearance data now, not a camera override.
    await set_workflow_config("image_gen", {**CONFIG, "pov_mode": "first"})
    events = await _trigger(client, "ig-pov-meta", {"action": "generate", "message_id": mid, "style_id": "anime"})
    assert ("image_gen_done", {"attachment_id": None}) not in events

    assert seen["pov"] == "first_person"
    rows = await get_workflow_attachments_for_message(mid)
    metadata = json.loads(rows[0]["generation_metadata"])
    assert metadata["pov"] == "first_person"
    assert metadata["pov_source"] == "manual"
    # Also in the display half: generation_metadata is the replay record the UI
    # never reads, and a wrong camera has to be visible on the bad image itself.
    consumption = json.loads(rows[0]["consumption_metadata"])
    assert consumption["pov"] == "first_person"
    assert consumption["pov_source"] == "manual"


# ── cloud source ─────────────────────────────────────────────────────────────

CLOUD_CONFIG = {
    **CONFIG,
    "source": "cloud",
    "cloud": {
        "provider": "xai",
        "width": 1024,
        "height": 1024,
        "providers": {"xai": {"api_key": "sk-live-canary", "model": "grok-imagine-image"}},
    },
}


@pytest.mark.asyncio
async def test_status_under_cloud_reports_the_provider_and_never_the_key(client):
    await _seed("ig-cloud-status")
    await set_workflow_config("image_gen", CLOUD_CONFIG)

    status = (await client.post("/api/workflows/image_gen/query", json={"action": "status"})).json()

    assert status["source"] == "cloud"
    assert status["ready"] is True
    assert "grok-imagine-image" in status["detail"]
    # The provider list is the preset table projected. A configured credential
    # reaching this payload would put it on the wire on every tools-panel open.
    assert "sk-live-canary" not in json.dumps(status)


@pytest.mark.asyncio
async def test_node_types_still_answers_while_cloud_is_selected(client, monkeypatch):
    """Imported graphs are global and the importer must stay usable after a backend
    switch, so this dispatches to ComfyUI by name rather than by active source."""
    await _seed("ig-cloud-nodes")
    await set_workflow_config("image_gen", CLOUD_CONFIG)

    async def fake_roles(_self, class_types):
        return {name: {"output_node": False, "text_inputs": ["text"]} for name in class_types}

    monkeypatch.setattr(
        "backend.workflows.image_gen.engine.adapters.external_comfy.ExternalComfyAdapter.node_roles", fake_roles
    )

    answer = (
        await client.post(
            "/api/workflows/image_gen/query",
            json={"action": "node_types", "class_types": ["CLIPTextEncode"]},
        )
    ).json()
    assert answer["nodes"]["CLIPTextEncode"]["text_inputs"] == ["text"]


@pytest.mark.asyncio
async def test_an_unreachable_comfy_server_degrades_node_types_instead_of_erroring(client, monkeypatch):
    await _seed("ig-cloud-nodes-down")
    await set_workflow_config("image_gen", CLOUD_CONFIG)

    async def boom(_self, class_types):
        raise ImageGenerationError("Could not communicate with ComfyUI")

    monkeypatch.setattr("backend.workflows.image_gen.engine.adapters.external_comfy.ExternalComfyAdapter.node_roles", boom)

    answer = (
        await client.post(
            "/api/workflows/image_gen/query",
            json={"action": "node_types", "class_types": ["CLIPTextEncode"]},
        )
    ).json()
    # The frontend already catches to {}, so a hard error here would only turn a
    # degraded importer into a broken one.
    assert answer == {"nodes": {}}


async def _seed_cloud_attachment(conv_id: str, *, seed: str, evicted: bool = False) -> tuple[int, int]:
    mid = await _seed(conv_id)
    await set_workflow_config("image_gen", CLOUD_CONFIG)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.webp",
            "mime": "image/webp",
            "data": b"RIFF0000WEBPold",
            "workflow_id": "image_gen",
            "seed": seed,
            "generation_metadata": {
                "style_id": "anime",
                "prompt": "1girl, standing",
                "negative_prompt": "",
                "source": "cloud",
                "backend_model": "grok-imagine-image",
                "width": 1024,
                "height": 1024,
                "seed_honored": False,
            },
            "consumption_metadata": {"style_id": "anime", "style_label": "Anime"},
        },
    )
    if evicted:
        from backend.workflows.attachment_cache import evict

        await evict(aid)
    return mid, aid


def _cloud_render(monkeypatch):
    async def fake_generate(_self, request, *, target, progress=None):
        return ImageResult(
            image_bytes=b"RIFF0000WEBPnew",
            mime="image/webp",
            backend_info={"source": "cloud", "workflow_id": None, "seed_honored": False, "notes": []},
        )

    monkeypatch.setattr(
        "backend.workflows.image_gen.engine.adapters.openai_image.OpenAICompatibleImageAdapter.generate", fake_generate
    )


@pytest.mark.asyncio
async def test_rehydrating_a_seedless_render_discloses_that_it_is_a_fresh_image(client, monkeypatch):
    """Rehydrate promises the same bytes back. A seedless API cannot give them, so
    the row is overwritten with something the user never generated -- and billed."""
    mid, aid = await _seed_cloud_attachment("ig-cloud-rehydrate", seed="1234", evicted=True)
    _cloud_render(monkeypatch)

    response = await client.post(
        f"/api/conversations/ig-cloud-rehydrate/messages/{mid}/workflow-attachments/{aid}/rehydrate", json={}
    )

    assert response.status_code == 200
    rows = await get_workflow_attachments_for_message(mid)
    row = next(r for r in rows if r["id"] == aid)
    notes = json.loads(row["consumption_metadata"])["notes"]
    assert any("could not be restored exactly" in note for note in notes)


@pytest.mark.asyncio
async def test_rerolling_an_evicted_attachment_does_not_claim_to_be_a_rehydrate(client, monkeypatch):
    """The case the eviction-marker discriminator gets wrong. `/reroll-gen` has no
    bytes precondition and the widget renders its button beside the evicted card,
    so this is one click away -- and a reroll of a seedless render is exactly what
    a reroll is supposed to be, not a broken restore."""
    mid, aid = await _seed_cloud_attachment("ig-cloud-reroll-evicted", seed="1234", evicted=True)
    _cloud_render(monkeypatch)

    response = await client.post(
        f"/api/conversations/ig-cloud-reroll-evicted/messages/{mid}/workflow-attachments/{aid}/reroll-gen", json={}
    )

    assert response.status_code == 200
    rows = await get_workflow_attachments_for_message(mid)
    sibling = next(r for r in rows if r["id"] != aid)
    notes = json.loads(sibling["consumption_metadata"]).get("notes") or []
    assert not any("restored exactly" in note for note in notes), notes


@pytest.mark.asyncio
async def test_rerolling_a_comfy_image_under_cloud_discloses_the_source_change(client, monkeypatch):
    mid = await _seed("ig-cross-source")
    await set_workflow_config("image_gen", CLOUD_CONFIG)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"\x89PNG\r\n\x1a\nold",
            "workflow_id": "image_gen",
            "seed": "1234",
            "generation_metadata": {
                "style_id": "anime",
                "prompt": "1girl",
                "negative_prompt": "",
                "source": "external_comfy",
                "workflow_id": "user_a",
            },
        },
    )
    _cloud_render(monkeypatch)

    response = await client.post(
        f"/api/conversations/ig-cross-source/messages/{mid}/workflow-attachments/{aid}/reroll-gen", json={}
    )

    assert response.status_code == 200
    rows = await get_workflow_attachments_for_message(mid)
    sibling = next(r for r in rows if r["id"] != aid)
    notes = json.loads(sibling["consumption_metadata"])["notes"]
    assert any("External ComfyUI" in note and "xAI" in note for note in notes), notes


@pytest.mark.asyncio
async def test_a_cloud_generate_runs_the_whole_stack_end_to_end(client, monkeypatch):
    """Trigger route -> hooks -> router -> cloud adapter -> providers builder ->
    HTTP client -> stored attachment, with only the prompter LLM stubbed.

    Every other test on this path stubs `resolve_and_generate` or the adapter's
    `generate`, which proves the plumbing around the render but not the render.
    """
    import base64
    import io
    import json as _json

    import httpx
    from PIL import Image

    from backend.workflows.image_gen.engine.adapters import openai_image
    from backend.workflows.image_gen.engine.openai_image_client import OpenAIImageClient

    mid = await _seed("ig-cloud-e2e")
    await set_workflow_config("image_gen", {**CLOUD_CONFIG, "cloud": {**CLOUD_CONFIG["cloud"], "width": 1536, "height": 1024}})

    buf = io.BytesIO()
    Image.new("RGB", (1344, 768), (12, 24, 48)).save(buf, format="PNG")
    submitted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        submitted["path"] = request.url.path
        submitted["auth"] = request.headers.get("authorization")
        submitted["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}], "usage": {"cost_in_usd_ticks": 900}},
        )

    def fake_client(self, timeout):
        return OpenAIImageClient(
            "https://api.x.ai/v1", "sk-live-canary", label=self.label, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(openai_image.OpenAICompatibleImageAdapter, "_client", fake_client)

    seen: dict = {}

    async def fake_compose(**kwargs):
        seen.update(kwargs)
        return "1girl, standing by the window", "", "single_call"

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)

    events = await _trigger(client, "ig-cloud-e2e", {"action": "generate", "message_id": mid, "style_id": "anime"})

    assert ("image_gen_done", {"attachment_id": None}) not in events
    # The provider is named in the phase pill rather than ComfyUI's wording.
    assert any(data.get("label") == "Rendering on xAI (Grok)..." for name, data in events if name == "phase_status")

    # The prompter was told not to write a negative, because xAI would discard it.
    assert seen["supports_negative"] is False

    assert submitted["path"] == "/v1/images/generations"
    assert submitted["auth"] == "Bearer sk-live-canary"
    # 1536x1024 is 3:2 exactly, and `size` is the spelling xAI rejects outright.
    assert submitted["body"]["aspect_ratio"] == "3:2"
    assert "size" not in submitted["body"]
    assert "negative_prompt" not in submitted["body"]
    assert "seed" not in submitted["body"]
    assert submitted["body"]["n"] == 1

    rows = await get_workflow_attachments_for_message(mid)
    metadata = _json.loads(rows[0]["generation_metadata"])
    assert metadata["source"] == "cloud"
    assert metadata["backend_model"] == "grok-imagine-image"
    # Real pixels, probed off what came back -- not the 1536x1024 that was asked for.
    assert (metadata["width"], metadata["height"]) == (1344, 768)
    assert metadata["seed_honored"] is False
    # The seed is still stored: rehydrate 409s on a null one, which would make
    # every cloud image permanently unrehydratable.
    assert rows[0]["seed"]

    consumption = _json.loads(rows[0]["consumption_metadata"])
    assert consumption["source"] == "xAI (Grok)"
    assert consumption["cost"] == {"provider": "xai", "unit": "usd_ticks", "value": 900}
    # The negative is recorded even though it was never sent, so replaying this
    # image on ComfyUI later is still correct.
    assert "negative_prompt" in consumption
