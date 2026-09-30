"""The image-gen composer's forced calls ride the conversation's cached prefix.

Unlike the other image_gen tests, this one does NOT stub ``compose_scene`` —
only the ComfyUI renderer. The select/compose forced calls flow through the
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

import base64
import io
import json

from PIL import Image

from backend.database import (
    create_lorebook_entry,
    create_user_persona,
    create_world,
    get_messages,
    get_workflow_attachment_by_id,
    update_settings,
)
from backend.workflows import set_workflow_character_state, set_workflow_config
from backend.workflows.image_gen.config import DEFAULT_SCENE_SKILLS
from backend.workflows.image_gen.engine import ImageResult


def _tc(name: str, args: dict) -> list[dict]:
    return [{"id": "t1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]


async def _armed_conversation(client, llm_mock) -> tuple[str, str]:
    """A conversation with one genuine chat turn on a writer lane and a separate agent lane.

    Returns ``(conversation_id, card_id)``.
    """
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
    return cid, card_id


async def _fake_render(adapter, request, **kwargs):
    return ImageResult(
        image_bytes=b"\x89PNG\r\n\x1a\nimage",
        mime="image/png",
        backend_info={
            "source": "external_comfy",
            "workflow_id": "user_a",
            "backend_model": "a.safetensors",
        },
    )


async def test_composer_forced_calls_ride_the_turn_prefix(client, llm_mock, monkeypatch):
    cid, card_id = await _armed_conversation(client, llm_mock)

    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "scene_skills_enabled": True,  # both forced calls (select + compose) must fire
            "prompter_reasoning": True,
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    await set_workflow_character_state(card_id, "image_gen", {"appearance_prompt": "long silver hair"})

    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", _fake_render)

    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "read_image_skills",
                {
                    "skill_ids": ["first_person_hug"],
                    "visible_subjects": ["Iris"],
                },
            )
        }
    )
    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "compose_image_prompt",
                {"scene": "1girl, sitting, window, rain, night", "avoid": "", "visible_subjects": ["Iris"]},
            )
        }
    )

    msgs = await get_messages(cid)
    mid = next(m["id"] for m in reversed(msgs) if m["role"] == "assistant")
    resp = await client.post(
        f"/api/conversations/{cid}/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )
    assert resp.status_code == 200
    assert "event: image_gen_done" in resp.text
    attachment_id = int(resp.text.partition('"attachment_id":')[2].partition("}")[0])
    attachment = await get_workflow_attachment_by_id(attachment_id)
    generation = json.loads(attachment["generation_metadata"])
    consumption = json.loads(attachment["consumption_metadata"])
    # Read the label off the shipped library rather than restating it: the recorded
    # metadata must track the seeded skill, and the wording is retuned often.
    hug = next(skill for skill in DEFAULT_SCENE_SKILLS if skill["id"] == "first_person_hug")
    expected_skills = [{"id": hug["id"], "label": hug["label"]}]
    assert generation["composition_skills"] == expected_skills
    assert consumption["composition_skills"] == expected_skills

    # Vacuity guards: the forced calls really reached the client boundary, ship
    # the workflow's own tools blob and force via tool_choice (the pipeline
    # pattern — a chat model needs the real tool, not tools=None), and share the
    # conversation identity the teardown invariant groups by — so system-prefix
    # parity with the chat turn is enforced there for every off-turn call site.
    wf = [c for c in llm_mock.captured if c["pass"] == "workflow"]
    assert len(wf) == 2, "composer must issue select + compose through the real forced-call stack"
    writer = next(c for c in llm_mock.captured if c["pass"] == "writer")
    agent_pass = next(c for c in llm_mock.captured if c["pass"] == "director")
    assert writer["endpoint"] == "http://writer.local"
    assert writer["model"] == "writer-model"
    blobs = set()
    for c in wf:
        names = [t["function"]["name"] for t in (c["tools"] or [])]
        assert names == ["read_image_skills", "compose_image_prompt", "refine_image_prompt"], (
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
    assert len(blobs) == 1, "select and compose must ship the byte-identical blob so they reuse each other's prefix"


async def test_each_review_extends_the_thread_before_it(client, llm_mock, monkeypatch):
    """Refinement is one growing thread on the agent lane: a review must resend every
    earlier call byte for byte and add only its own turn, or each review re-bills the
    renders before it. Each replayed call is answered by exactly one tool result, which
    says when the render came from a new seed but never which seed."""
    cid, card_id = await _armed_conversation(client, llm_mock)
    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "refine_turns": 2,
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    await set_workflow_character_state(card_id, "image_gen", {"appearance_prompt": "long silver hair"})
    seeds: list[int] = []

    async def render(adapter, request, **kwargs):
        seeds.append(request.seed)
        return await _fake_render(adapter, request, **kwargs)

    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render)
    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "compose_image_prompt",
                {"scene": "1girl, sitting, window, rain", "avoid": "", "visible_subjects": ["Iris"]},
            )
        }
    )
    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "refine_image_prompt",
                {
                    "critique": "no rain visible",
                    "done": False,
                    "reseed": True,
                    "scene": "1girl, sitting, window, heavy rain",
                    "avoid": None,
                },
            )
        }
    )
    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "refine_image_prompt", {"critique": "", "done": True, "reseed": False, "scene": None, "avoid": None}
            )
        }
    )

    mid = next(m["id"] for m in reversed(await get_messages(cid)) if m["role"] == "assistant")
    resp = await client.post(
        f"/api/conversations/{cid}/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )
    assert resp.status_code == 200
    assert resp.text.count("event: image_gen_render") == 2

    wf = [c for c in llm_mock.captured if c["pass"] == "workflow"]
    assert [c["tool_choice"]["function"]["name"] for c in wf] == [
        "compose_image_prompt",
        "refine_image_prompt",
        "refine_image_prompt",
    ]
    assert len({json.dumps(c["tools"], sort_keys=True) for c in wf}) == 1
    results = []
    for earlier, later in zip(wf, wf[1:], strict=False):
        sent = earlier["messages"]
        assert later["messages"][: len(sent)] == sent, "a review must extend the call before it, byte for byte"
        call, result, image = later["messages"][len(sent) :]
        assert call["role"] == "assistant" and result["role"] == "tool" and image["role"] == "user"
        assert result["tool_call_id"] == call["tool_calls"][0]["id"]
        results.append(result["content"])
        assert image["content"][0]["image_url"]["url"].startswith("data:image/")
    assert len(set(seeds)) == 2, "the first review asked for a new seed"
    assert ["new seed" in text for text in results] == [False, True]
    assert not any(str(seed) in text for seed in seeds for text in results)
    ids = [m["tool_calls"][0]["id"] for m in wf[-1]["messages"] if m.get("tool_calls")]
    assert ids == ["imgcall00", "imgcall01"], "synthesized ids must be nine alphanumerics; Mistral rejects any other shape"


def _png() -> bytes:
    """A real image: the prompter's copy is re-encoded, so fake bytes would not decode."""
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buffer, "PNG")
    return buffer.getvalue()


async def _png_render(adapter, request, **kwargs):
    return ImageResult(image_bytes=_png(), mime="image/png", backend_info={"source": "external_comfy"})


def _generate(client, cid: str, mid: int):
    return client.post(
        f"/api/conversations/{cid}/workflows/image_gen/trigger",
        json={"action": "generate", "message_id": mid, "style_id": "anime"},
    )


async def test_the_earlier_chat_image_rides_the_compose_tail_not_the_prefix(client, llm_mock, monkeypatch):
    """Prompter reference puts the chat's last generated image in front of the compose
    request, where each review already puts its render, without its saved prompts:
    the shared prefix the selector used stays byte-identical, and the first review
    re-sends the image-bearing tail."""
    cid, _card_id = await _armed_conversation(client, llm_mock)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", _png_render)
    base = {"source": "external_comfy", "default_style": "anime", "prompter_reference": True}
    await set_workflow_config("image_gen", {**base, "external_comfy": {"api_url": "http://127.0.0.1:8188"}})
    greeting, reply = [m["id"] for m in await get_messages(cid) if m["role"] == "assistant"]

    llm_mock.enqueue_workflow(
        {"tool_calls": _tc("compose_image_prompt", {"scene": "1girl, earlier_scene_marker", "avoid": "earlier_avoid_marker"})}
    )
    first = await _generate(client, cid, greeting)
    assert "event: image_gen_done" in first.text
    first_id = int(first.text.partition('"attachment_id":')[2].partition("}")[0])
    assert isinstance(llm_mock.captured[-1]["messages"][-1]["content"], str), "nothing came before the greeting"

    await set_workflow_config(
        "image_gen",
        {
            **base,
            "scene_skills_enabled": True,
            "refine_turns": 1,
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    llm_mock.enqueue_workflow({"tool_calls": _tc("read_image_skills", {"skill_ids": [], "visible_subjects": ["Iris"]})})
    llm_mock.enqueue_workflow({"tool_calls": _tc("compose_image_prompt", {"scene": "1girl, window, rain", "avoid": ""})})
    llm_mock.enqueue_workflow(
        {
            "tool_calls": _tc(
                "refine_image_prompt", {"critique": "", "done": True, "reseed": False, "scene": None, "avoid": None}
            )
        }
    )
    second = await _generate(client, cid, reply)
    assert "event: image_gen_done" in second.text

    select, compose, review = [c for c in llm_mock.captured if c["pass"] == "workflow"][-3:]
    assert isinstance(select["messages"][-1]["content"], str), "the selector stays text-only"
    assert compose["messages"][:-1] == select["messages"][:-1], "the image must not move into the shared prefix"
    image, request = compose["messages"][-1]["content"]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "picture from earlier in this chat" in request["text"]
    earlier = json.loads((await get_workflow_attachment_by_id(first_id))["generation_metadata"])
    assert "earlier_scene_marker" in earlier["prompt"]
    assert "earlier_avoid_marker" in earlier["negative_prompt"]
    assert "earlier_scene_marker" not in request["text"]
    assert "earlier_avoid_marker" not in request["text"]
    assert "Earlier prompt:" not in request["text"]
    assert "Earlier negative prompt:" not in request["text"]
    assert review["messages"][: len(compose["messages"])] == compose["messages"], "the review re-sends the tail byte for byte"
    rendered_id = int(second.text.partition('"attachment_id":')[2].partition("}")[0])
    generation = json.loads((await get_workflow_attachment_by_id(rendered_id))["generation_metadata"])
    assert generation["prompter_reference"] == f"attachment:{first_id}"


async def test_an_upload_is_not_sent_to_the_prompter_twice(client, llm_mock, monkeypatch):
    """A user's upload already reaches the prompter as pixels in the prefix."""
    cid, _card_id = await _armed_conversation(client, llm_mock)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", _png_render)
    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "prompter_reference": True,
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    llm_mock.enqueue_writer("She studies the map.")
    llm_mock.enqueue_editor(None)
    upload = {"b64": base64.b64encode(_png()).decode("ascii"), "mime": "image/png", "filename": "map.png"}
    resp = await client.post(f"/api/conversations/{cid}/send", json={"content": "Look.", "attachments": [upload]})
    assert resp.status_code == 200
    _ = resp.text
    reply = next(m["id"] for m in reversed(await get_messages(cid)) if m["role"] == "assistant")

    llm_mock.enqueue_workflow({"tool_calls": _tc("compose_image_prompt", {"scene": "1girl, map", "avoid": ""})})
    assert "event: image_gen_done" in (await _generate(client, cid, reply)).text

    compose = [c for c in llm_mock.captured if c["pass"] == "workflow"][-1]
    assert isinstance(compose["messages"][-1]["content"], str)


async def test_an_image_older_than_the_last_reply_is_not_sent_to_the_prompter(client, llm_mock, monkeypatch):
    """A picture from before the last reply shows a scene the story has left."""
    cid, _card_id = await _armed_conversation(client, llm_mock)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", _png_render)
    await set_workflow_config(
        "image_gen",
        {
            "source": "external_comfy",
            "default_style": "anime",
            "prompter_reference": True,
            "external_comfy": {"api_url": "http://127.0.0.1:8188"},
        },
    )
    greeting = next(m["id"] for m in await get_messages(cid) if m["role"] == "assistant")
    llm_mock.enqueue_workflow({"tool_calls": _tc("compose_image_prompt", {"scene": "1girl, archive", "avoid": ""})})
    assert "event: image_gen_done" in (await _generate(client, cid, greeting)).text
    llm_mock.enqueue_writer("She leaves the archive.")
    llm_mock.enqueue_editor(None)
    resp = await client.post(f"/api/conversations/{cid}/send", json={"content": "Go on."})
    assert resp.status_code == 200
    _ = resp.text
    latest = next(m["id"] for m in reversed(await get_messages(cid)) if m["role"] == "assistant")

    llm_mock.enqueue_workflow({"tool_calls": _tc("compose_image_prompt", {"scene": "1girl, street", "avoid": ""})})
    assert "event: image_gen_done" in (await _generate(client, cid, latest)).text

    compose = [c for c in llm_mock.captured if c["pass"] == "workflow"][-1]
    assert isinstance(compose["messages"][-1]["content"], str)
    assert "Earlier prompt" not in compose["messages"][-1]["content"]
