from __future__ import annotations

import json
import re

import pytest

from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    get_workflow_attachment_by_id,
)
from backend.workflows import (
    HookType,
    get_workflow,
    set_workflow_character_state,
    set_workflow_config,
    workflow_has_hook,
)
from backend.workflows.image_gen.engine import ImageResult


@pytest.mark.asyncio
async def test_manifest_and_status_expose_external_only_stage(client):
    manifest = (await client.get("/api/workflows")).json()
    entry = next(w for w in manifest if w["id"] == "image_gen")
    assert entry["display_name"] == "Image Generation"

    status = (await client.get("/api/workflows/image_gen/status")).json()
    assert status["source"] == "external_comfy"
    assert status["capabilities"] == {
        "can_generate": True,
        "can_list_models": True,
        "can_install_curated_models": False,
        "managed_runtime": False,
    }


def test_image_generation_is_on_demand_only():
    workflow = get_workflow("image_gen")
    assert workflow is not None
    assert workflow_has_hook(workflow, HookType.ON_DEMAND)
    assert not workflow_has_hook(workflow, HookType.POST_PIPELINE)


@pytest.mark.asyncio
async def test_generate_trigger_streams_terminal_event_and_persists_image(client, monkeypatch):
    await create_character_card({"id": "ig-char", "name": "Iris"})
    await create_conversation("ig-conv", "Images", "Iris", "A moonlit room", character_card_id="ig-char")
    mid, _ = await add_message("ig-conv", "assistant", "Iris sits beside the rain-streaked window.", 0)
    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "external_comfy": {
                "api_url": "http://127.0.0.1:8188",
                "checkpoint": "anime.safetensors",
                "workflow": "external_core",
            },
        },
    )
    await set_workflow_character_state("ig-char", "image_gen", {"appearance_prompt": "long silver hair"})

    async def fake_compose(**kwargs):
        return "1girl, sitting, window, rain, night", "day", "single_call"

    captured = {}

    async def fake_render(config, request, **kwargs):
        captured["request"] = request
        return ImageResult(
            image_bytes=b"\x89PNG\r\n\x1a\nimage",
            mime="image/png",
            backend_info={
                "source": "external_comfy",
                "workflow_id": "external_core",
                "backend_model": "anime.safetensors",
            },
        )

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    response = await client.post(
        "/api/conversations/ig-conv/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )
    assert response.status_code == 200
    assert "event: image_gen_done" in response.text
    assert '"attachment_id":' in response.text
    assert captured["request"].prompt.startswith("long silver hair, 1girl")
    assert captured["request"].prompt.endswith("anime illustration, clean line art, very aesthetic, high contrast")

    match = re.search(r'"attachment_id":(\d+)', response.text)
    assert match
    attachment = await get_workflow_attachment_by_id(int(match.group(1)))
    assert attachment is not None
    assert attachment["mime_type"] == "image/png"
    assert attachment["seed"]
    assert json.loads(attachment["generation_metadata"])["backend_model"] == "anime.safetensors"
