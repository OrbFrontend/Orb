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
    writer_endpoint = await client.post("/api/endpoints", json={"url": "http://writer.local", "api_key": "writer-key"})
    assert writer_endpoint.status_code == 200
    writer_config_id = writer_endpoint.json()["active_model_config_id"]
    writer_model = await client.put(
        f"/api/models/{writer_config_id}",
        json={"model_name": "writer-model"},
    )
    assert writer_model.status_code == 200

    agent_endpoint = await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "agent-key"})
    assert agent_endpoint.status_code == 200
    agent_config_id = agent_endpoint.json()["agent_active_model_config_id"]
    agent_model = await client.put(
        f"/api/models/{agent_config_id}",
        json={"model_name": "agent-model", "reasoning_effort": "high"},
    )
    assert agent_model.status_code == 200

    resp = await client.put(
        "/api/settings",
        json={
            "active_endpoint_id": writer_endpoint.json()["id"],
            "enable_agent": True,
            "agent_same_as_writer": False,
            "agent_endpoint_id": agent_endpoint.json()["id"],
            "agent_shared_system_prompt": "Agent-only system prompt.",
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
            "prompter_reasoning": True,
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    await set_workflow_character_state(card_id, "image_gen", {"appearance_prompt": "long silver hair"})

    async def fake_render(config, request, **kwargs):
        return ImageResult(
            image_bytes=b"\x89PNG\r\n\x1a\nimage",
            mime="image/png",
            backend_info={
                "source": "external_comfy",
                "workflow_id": "user_a",
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

    # Vacuity guards: the forced calls really reached the client boundary, ship
    # the workflow's own tools blob and force via tool_choice (the pipeline
    # pattern — a chat model needs the real tool, not tools=None), and share the
    # conversation identity the teardown invariant groups by — so system-prefix
    # parity with the chat turn is enforced there for every off-turn call site.
    wf = [c for c in llm_mock.captured if c["pass"] == "workflow"]
    assert len(wf) == 2, "composer must issue analyze + compose through the real forced-call stack"
    writer = next(c for c in llm_mock.captured if c["pass"] == "writer")
    agent_pass = next(c for c in llm_mock.captured if c["pass"] == "director")
    assert writer["endpoint"] == "http://writer.local"
    assert writer["model"] == "writer-model"
    blobs = set()
    for c in wf:
        names = [t["function"]["name"] for t in (c["tools"] or [])]
        assert names == ["analyze_scene", "compose_image_prompt"], (
            "off-turn calls must ship the workflow's own tools blob, not tools=None — "
            "most chat models won't reliably call a tool they were never given"
        )
        assert isinstance(c["tool_choice"], dict), "the off-turn call forces its tool via tool_choice"
        assert c["endpoint"] == "http://agent.local"
        assert c["model"] == "agent-model"
        assert c["messages"][0] == agent_pass["messages"][0]
        assert c["messages"][0] != writer["messages"][0]
        assert c["params"]["reasoning"] == {"enabled": True}
        assert c["params"]["thinking"] == {"type": "enabled"}
        blobs.add(json.dumps(c["tools"], sort_keys=True))
        assert c["messages"][1] == agent_pass["messages"][1], (
            "off-turn call lost the conversation's history head — the teardown invariant would "
            "silently group it apart from the chat turn instead of comparing prefixes"
        )
    assert len(blobs) == 1, "analyze and compose must ship the byte-identical blob so they reuse each other's prefix"
