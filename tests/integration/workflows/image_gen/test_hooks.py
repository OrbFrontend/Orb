"""On-demand guards, replay routing, and the on-demand-only contract.

The generate action returns a `StreamingResponse` whose body is consumed after the
trigger route has released its locks, so every one of these asserts against the
*stream* rather than a JSON body -- a guard that answers with plain JSON here is
invisible to the client, which is parsing SSE frames.
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
_CLOUD_GENERATE = "backend.workflows.image_gen.engine.adapters.openai_image.OpenAICompatibleImageAdapter.generate"

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
    "external_comfy": {"api_url": "http://127.0.0.1:8188", "user_graphs": [USER_GRAPH]},
}

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


async def _seed(conv_id: str, *, with_character: bool = False, config: dict | None = None) -> int:
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
    await set_workflow_config("image_gen", config or CONFIG)
    return mid


async def _attach(mid: int, *, seed: str = "1234", consumption: dict | None = None, webp: bool = False, **generation) -> int:
    """One stored image_gen attachment, with the generation parameters a replay reads."""
    row = {
        "filename": "x.webp" if webp else "x.png",
        "mime": "image/webp" if webp else "image/png",
        "data": b"RIFF0000WEBPold" if webp else b"\x89PNG\r\n\x1a\nold",
        "workflow_id": "image_gen",
        "seed": seed,
        "generation_metadata": {"style_id": "anime", "prompt": "1girl", "negative_prompt": "", **generation},
    }
    if consumption is not None:
        row["consumption_metadata"] = consumption
    return await insert_workflow_attachment_row(mid, row)


async def _trigger(client, conv_id: str, body: dict) -> list[tuple[str, dict]]:
    response = await client.post(f"/api/conversations/{conv_id}/workflows/image_gen/trigger", json=body)
    assert response.status_code == 200
    return _events(response.text)


async def _replay(client, conv_id: str, mid: int, aid: int, action: str = "reroll-gen", **body):
    response = await client.post(f"/api/conversations/{conv_id}/messages/{mid}/workflow-attachments/{aid}/{action}", json=body)
    assert response.status_code == 200
    return response


async def _sibling(mid: int, aid: int) -> dict:
    rows = await get_workflow_attachments_for_message(mid)
    return next(row for row in rows if row["id"] != aid)


def _stub(monkeypatch, render=None, scene="1girl, standing"):
    async def fake_compose(**kwargs):
        return scene, "", "single_call"

    async def fake_render(config, request, **kwargs):
        return _image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render or fake_render)


def _capture_comfy(monkeypatch) -> dict:
    """Swap the ComfyUI render for one recording what it was actually targeted at."""
    captured: dict = {}

    async def capture(_self, request, *, target, progress=None):
        captured.update(checkpoint=target.model, graph_id=target.target_id, seed=request.seed, prompt=request.prompt)
        return _image(notes=list(target.notes))

    monkeypatch.setattr(_COMFY_GENERATE, capture)
    return captured


def _cloud_render(monkeypatch):
    async def fake_generate(_self, request, *, target, progress=None):
        return ImageResult(
            image_bytes=b"RIFF0000WEBPnew",
            mime="image/webp",
            backend_info={"source": "cloud", "workflow_id": None, "seed_honored": False, "notes": []},
        )

    monkeypatch.setattr(_CLOUD_GENERATE, fake_generate)


# ── guards ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_key, message",
    [("bad_type", "message_id (int) required"), ("missing", "no longer part of this conversation")],
)
async def test_a_rejected_generate_still_speaks_the_stream_contract(client, monkeypatch, body_key, message):
    """A guard that answers in JSON leaves the client waiting on frames that never
    come: it sees no terminal event and re-enables its button silently."""
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
    """Conversation membership is not branch membership: composing from a history
    that never reaches the anchor describes replies that came after it."""
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

    assert [name for name, _ in events][-1] == "image_gen_done"
    assert events[-1][1] == {"attachment_id": None}
    assert any(name == "image_gen_error" and "could not complete" in data["message"] for name, data in events)


@pytest.mark.asyncio
async def test_two_concurrent_triggers_on_one_message_stay_separate_roots(client, monkeypatch):
    """The stream body runs after the trigger route drops its locks, so nothing
    serializes these two. What must hold is that each lands as its own flat
    attachment root rather than interleaving into a corrupt sibling tree."""
    mid = await _seed("ig-race")

    async def slow_render(config, request, **kwargs):
        await asyncio.sleep(0.05)
        return _image()

    _stub(monkeypatch, render=slow_render)
    body = {"action": "generate", "message_id": mid}

    first, second = await asyncio.gather(_trigger(client, "ig-race", body), _trigger(client, "ig-race", body))

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
    aid = await _attach(mid, prompt="1girl, standing", workflow_id="user_a", backend_model="original.safetensors")
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-reroll", mid, aid)

    assert captured["checkpoint"] == "original.safetensors"
    assert captured["graph_id"] == "user_a"
    assert captured["seed"] != 1234, "a reroll must move the seed or it silently returns the cached image"


@pytest.mark.asyncio
async def test_a_style_override_drops_the_old_style_pins_and_discloses_the_wording(client, monkeypatch):
    """Swapping style retargets the render; the pins describe the OLD style, so they
    go. The prompt text cannot follow -- only the assembled string is stored -- so
    say so."""
    mid = await _seed("ig-swap")
    aid = await _attach(
        mid,
        workflow_id="user_a",
        backend_model="original.safetensors",
        consumption={"style_id": "anime", "style_label": "Anime"},
    )
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-swap", mid, aid, params={"style_id": "realistic"})

    assert captured["graph_id"] == ""  # the realistic style pins neither
    assert captured["checkpoint"] == ""
    sibling = await _sibling(mid, aid)
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
    mid = await _seed("ig-noswap")
    aid = await _attach(
        mid,
        workflow_id="user_a",
        backend_model="original.safetensors",
        consumption={"style_id": "anime", "style_label": "Anime"},
    )
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-noswap", mid, aid, params={"style_id": "anime", "prompt": "edited"})

    assert captured["graph_id"] == "user_a"
    assert captured["checkpoint"] == "original.safetensors"
    assert captured["prompt"] == "edited"
    assert "notes" not in json.loads((await _sibling(mid, aid))["consumption_metadata"])


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

    aid = await _attach(mid, style_id="realistic", prompt="stale")
    captured: dict = {}

    async def fake_compose(**kwargs):
        captured.update(kwargs)
        return "1girl, doorway, looking back", "", "single_call"

    _stub(monkeypatch)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)

    await _replay(client, "ig-regen", mid, aid, action="regenerate")

    sibling = await _sibling(mid, aid)
    assert sibling["parent_attachment_id"] == aid
    metadata = json.loads(sibling["generation_metadata"])
    # Recomposed from current settings, not replayed from the predecessor: both its
    # stored prompt and its realistic style are left behind.
    assert metadata["style_id"] == "anime"
    assert "doorway" in metadata["prompt"] and "anime illustration" in metadata["prompt"]
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

    # The picker labels "Auto" off these two, so both must be answered whatever the
    # machine has on disk -- a dev box with the GGUF present reports ready, and an
    # absent flag would leave the label lying either way.
    status = (await client.post("/api/workflows/image_gen/query", json={"action": "status"})).json()
    assert isinstance(status["classifier_ready"], bool)
    assert status["fallback_mode"] == pov.DEFAULT_POV_MODE


@pytest.mark.asyncio
async def test_generate_records_the_camera_and_the_lever_that_chose_it(client, monkeypatch):
    mid = await _seed("ig-pov-meta", with_character=True)
    # The picker decides. A 'third_person' tag left in the character's appearance
    # prompt is appearance data now, not a camera override.
    await set_workflow_character_state("ig-pov-meta-char", "image_gen", {"appearance_prompt": "3D, third_person"})
    seen: dict = {}

    async def fake_compose(**kwargs):
        seen.update(kwargs)
        return "1girl, standing", "", "single_call"

    _stub(monkeypatch)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    await set_workflow_config("image_gen", {**CONFIG, "pov_mode": "first"})

    events = await _trigger(client, "ig-pov-meta", {"action": "generate", "message_id": mid, "style_id": "anime"})
    assert ("image_gen_done", {"attachment_id": None}) not in events

    assert seen["pov"] == "first_person"
    rows = await get_workflow_attachments_for_message(mid)
    metadata = json.loads(rows[0]["generation_metadata"])
    assert (metadata["pov"], metadata["pov_source"]) == ("first_person", "manual")
    # Also in the display half: generation_metadata is the replay record the UI
    # never reads, and a wrong camera has to be visible on the bad image itself.
    consumption = json.loads(rows[0]["consumption_metadata"])
    assert (consumption["pov"], consumption["pov_source"]) == ("first_person", "manual")


# ── cloud source ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_under_cloud_reports_the_provider_and_never_the_key(client):
    await _seed("ig-cloud-status", config=CLOUD_CONFIG)

    status = (await client.post("/api/workflows/image_gen/query", json={"action": "status"})).json()

    assert status["source"] == "cloud"
    assert status["ready"] is True
    assert "grok-imagine-image" in status["detail"]
    # The provider list is the preset table projected. A configured credential
    # reaching this payload would put it on the wire on every tools-panel open.
    assert "sk-live-canary" not in json.dumps(status)


@pytest.mark.asyncio
@pytest.mark.parametrize("reachable", [True, False], ids=["typed", "server down"])
async def test_node_types_still_answers_while_cloud_is_selected(client, monkeypatch, reachable):
    """Imported graphs are global and the importer must stay usable after a backend
    switch, so this dispatches to ComfyUI by name rather than by active source. An
    unreachable server degrades to no typing -- the frontend already catches to {},
    so a hard error here would only turn a degraded importer into a broken one."""
    await _seed(f"ig-cloud-nodes-{reachable}", config=CLOUD_CONFIG)

    async def fake_roles(_self, class_types):
        if not reachable:
            raise ImageGenerationError("Could not communicate with ComfyUI")
        return {name: {"output_node": False, "text_inputs": ["text"]} for name in class_types}

    monkeypatch.setattr(
        "backend.workflows.image_gen.engine.adapters.external_comfy.ExternalComfyAdapter.node_roles", fake_roles
    )

    answer = (
        await client.post("/api/workflows/image_gen/query", json={"action": "node_types", "class_types": ["CLIPTextEncode"]})
    ).json()
    assert answer == (
        {"nodes": {"CLIPTextEncode": {"output_node": False, "text_inputs": ["text"]}}} if reachable else {"nodes": {}}
    )


async def _seed_cloud_attachment(conv_id: str, *, evicted: bool = False) -> tuple[int, int]:
    mid = await _seed(conv_id, config=CLOUD_CONFIG)
    aid = await _attach(
        mid,
        webp=True,
        prompt="1girl, standing",
        source="cloud",
        backend_model="grok-imagine-image",
        width=1024,
        height=1024,
        seed_honored=False,
        consumption={"style_id": "anime", "style_label": "Anime"},
    )
    if evicted:
        from backend.workflows.attachment_cache import evict

        await evict(aid)
    return mid, aid


@pytest.mark.asyncio
async def test_rehydrating_a_seedless_render_discloses_that_it_is_a_fresh_image(client, monkeypatch):
    """Rehydrate promises the same bytes back. A seedless API cannot give them, so
    the row is overwritten with something the user never generated -- and billed."""
    mid, aid = await _seed_cloud_attachment("ig-cloud-rehydrate", evicted=True)
    _cloud_render(monkeypatch)

    await _replay(client, "ig-cloud-rehydrate", mid, aid, action="rehydrate")

    rows = await get_workflow_attachments_for_message(mid)
    row = next(r for r in rows if r["id"] == aid)
    assert any("takes no seed" in note for note in json.loads(row["consumption_metadata"])["notes"])


@pytest.mark.asyncio
async def test_rerolling_an_evicted_attachment_does_not_claim_to_be_a_rehydrate(client, monkeypatch):
    """The case the eviction-marker discriminator gets wrong. `/reroll-gen` has no
    bytes precondition and the widget renders its button beside the evicted card, so
    this is one click away -- and a reroll of a seedless render is exactly what a
    reroll is supposed to be, not a broken restore."""
    mid, aid = await _seed_cloud_attachment("ig-cloud-reroll-evicted", evicted=True)
    _cloud_render(monkeypatch)

    await _replay(client, "ig-cloud-reroll-evicted", mid, aid)

    notes = json.loads((await _sibling(mid, aid))["consumption_metadata"]).get("notes") or []
    assert not any("takes no seed" in note for note in notes), notes


@pytest.mark.asyncio
async def test_rerolling_a_comfy_image_under_cloud_discloses_the_source_change(client, monkeypatch):
    mid = await _seed("ig-cross-source", config=CLOUD_CONFIG)
    aid = await _attach(mid, source="external_comfy", workflow_id="user_a")
    _cloud_render(monkeypatch)

    await _replay(client, "ig-cross-source", mid, aid)

    notes = json.loads((await _sibling(mid, aid))["consumption_metadata"])["notes"]
    assert any("External ComfyUI" in note and "xAI" in note for note in notes), notes


@pytest.mark.asyncio
async def test_a_cloud_generate_runs_the_whole_stack_end_to_end(client, monkeypatch):
    """Trigger route -> hooks -> router -> cloud adapter -> providers builder -> HTTP
    client -> stored attachment, with only the prompter LLM stubbed. Every other test
    on this path stubs the render, which proves the plumbing around it but not it."""
    import base64
    import io

    import httpx
    from PIL import Image

    from backend.workflows.image_gen.engine.adapters import openai_image
    from backend.workflows.image_gen.engine.openai_image_client import OpenAIImageClient

    mid = await _seed(
        "ig-cloud-e2e", config={**CLOUD_CONFIG, "cloud": {**CLOUD_CONFIG["cloud"], "width": 1536, "height": 1024}}
    )

    buf = io.BytesIO()
    Image.new("RGB", (1344, 768), (12, 24, 48)).save(buf, format="PNG")
    submitted: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        submitted["path"] = request.url.path
        submitted["auth"] = request.headers.get("authorization")
        submitted["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}], "usage": {"cost_in_usd_ticks": 900}},
        )

    monkeypatch.setattr(
        openai_image.OpenAICompatibleImageAdapter,
        "_client",
        lambda self, timeout: OpenAIImageClient(
            "https://api.x.ai/v1", "sk-live-canary", label=self.label, transport=httpx.MockTransport(handler)
        ),
    )

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
    assert submitted["body"]["n"] == 1
    for absent in ("size", "negative_prompt", "seed"):
        assert absent not in submitted["body"]

    rows = await get_workflow_attachments_for_message(mid)
    metadata = json.loads(rows[0]["generation_metadata"])
    assert metadata["source"] == "cloud"
    assert metadata["backend_model"] == "grok-imagine-image"
    # Real pixels, probed off what came back -- not the 1536x1024 that was asked for.
    assert (metadata["width"], metadata["height"]) == (1344, 768)
    assert metadata["seed_honored"] is False
    # The seed is still stored: rehydrate 409s on a null one, which would make every
    # cloud image permanently unrehydratable.
    assert rows[0]["seed"]

    consumption = json.loads(rows[0]["consumption_metadata"])
    assert consumption["source"] == "xAI (Grok)"
    assert consumption["cost"] == {"provider": "xai", "unit": "usd_ticks", "value": 900}
    # The negative is recorded even though it was never sent, so replaying this image
    # on ComfyUI later is still correct.
    assert "negative_prompt" in consumption
