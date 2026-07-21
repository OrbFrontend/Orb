"""The image-gen composer's forced calls ride the conversation's cached prefix.

Unlike the other image_gen tests, this one does NOT stub ``compose_scene`` —
only the ComfyUI renderer. The analyze/compose forced calls flow through the
real ``build_offturn_prefix`` → ``forced_tool_call`` → ``client.complete()``
stack into ``FakeLLMClient``, alongside a genuine chat turn in the same
conversation. That is what arms the ``llm_mock`` teardown invariant
(``verify_kv_prefix_invariants``): both original leaks — the off-turn prefix
missing the constant-lorebook block, and per-call tool schemas rendered into
the prompt — would fail this test via the teardown check. The fixture seeds a
constant lorebook entry and an active persona so the system message has
content the off-turn builder must reproduce byte-for-byte.
"""

from __future__ import annotations

import json

from backend.database import (
    create_lorebook_entry,
    create_user_persona,
    create_world,
    get_messages,
    update_settings,
)
from backend.workflows import set_workflow_character_state, set_workflow_config
from backend.workflows.image_gen.engine import ImageResult


def _tc(name: str, args: dict) -> list[dict]:
    return [{"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]


async def test_composer_forced_calls_ride_the_turn_prefix(client, llm_mock, monkeypatch):
    resp = await client.put(
        "/api/settings",
        json={
            "model_name": "writer-model",
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
        },
    )
    assert resp.status_code == 200

    # Prefix-shaping state the off-turn builder must reproduce byte-for-byte.
    persona = await create_user_persona({"name": "Chi", "description": "A curious visitor."})
    await update_settings({"active_persona_id": persona["id"]})
    world = await create_world({"name": "Archive"})
    await create_lorebook_entry(
        world["id"],
        {"name": "Canon", "content": "The moon is shattered.", "constant": True},
    )

    card = await client.post(
        "/api/characters",
        json={
            "name": "Iris",
            "description": "{{char}} is a tired librarian who guards {{user}}.",
            "first_mes": "The archive is quiet tonight, {{user}}.",
            "scenario": "A rainy archive.",
        },
    )
    assert card.status_code == 200
    card_id = card.json()["id"]
    conv = await client.post("/api/conversations", json={"character_card_id": card_id})
    assert conv.status_code == 200
    cid = conv.json()["id"]

    # One genuine chat turn establishes the conversation's cached prefix.
    llm_mock.enqueue_writer("She sits by the rain-streaked window.")
    llm_mock.enqueue_editor(None)
    resp = await client.post(f"/api/conversations/{cid}/send", json={"content": "I step inside.", "attachments": []})
    assert resp.status_code == 200
    _ = resp.text

    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "scene_analysis": True,  # both forced calls (analyze + compose) must fire
            "external_comfy": {
                "api_url": "http://127.0.0.1:8188",
                "checkpoint": "a.safetensors",
                "workflow": "external_core",
            },
        },
    )
    await set_workflow_character_state(card_id, "image_gen", {"appearance_prompt": "long silver hair"})

    async def fake_render(config, request, **kwargs):
        return ImageResult(
            image_bytes=b"\x89PNG\r\n\x1a\nimage",
            mime="image/png",
            backend_info={
                "source": "external_comfy",
                "workflow_id": "external_core",
                "backend_model": "a.safetensors",
            },
        )

    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", fake_render)

    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "analyze_scene",
                {
                    "viewpoint": "third_person",
                    "characters": [{"name": "Iris", "appearance": ""}],
                    "setting": "rainy archive at night",
                },
            )
        }
    )
    llm_mock.enqueue_workflow(
        {"tool_calls": _tc("compose_image_prompt", {"scene": "1girl, sitting, window, rain, night", "avoid": ""})}
    )

    msgs = await get_messages(cid)
    mid = next(m["id"] for m in reversed(msgs) if m["role"] == "assistant")
    resp = await client.post(
        f"/api/conversations/{cid}/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )
    assert resp.status_code == 200
    assert "event: image_gen_done" in resp.text

    # Vacuity guards: the forced calls really reached the client boundary, keep
    # their schemas out of the prompt, and share the conversation identity the
    # teardown invariant groups by — so system-prefix parity with the chat turn
    # is enforced there for this and every future off-turn call site.
    wf = [c for c in llm_mock.captured if c["pass"] == "workflow"]
    assert len(wf) == 2, "composer must issue analyze + compose through the real forced-call stack"
    writer = next(c for c in llm_mock.captured if c["pass"] == "writer")
    for c in wf:
        assert c["params"].get("tools_in_prompt") is False, (
            "off-turn forced calls must not render tool schemas into the prompt (tools_in_prompt=False)"
        )
        assert c["messages"][1] == writer["messages"][1], (
            "off-turn call lost the conversation's history head — the teardown invariant would "
            "silently group it apart from the chat turn instead of comparing prefixes"
        )
