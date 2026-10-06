"""On-demand guards, replay routing, and the on-demand-only contract.

The generate action returns a `StreamingResponse` whose body is consumed after the trigger route has released its locks, so
every one of these asserts against the *stream* rather than a JSON body -- a guard that answers with plain JSON here is
invisible to the client, which is parsing SSE frames.
"""

import asyncio
import io
import json

import pytest
from PIL import Image

from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    get_workflow_attachment_by_id,
    get_workflow_attachments_for_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows import set_workflow_character_state, set_workflow_config
from backend.workflows.attachment_cache import insert_workflow_variant, set_active_sibling
from backend.workflows.image_gen import pov
from backend.workflows.image_gen.composer import Revision
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
        conv_id, "Images", "Iris", "A quiet room", character_card_id=f"{conv_id}-char" if with_character else None
    )
    mid, _ = await add_message(conv_id, "assistant", "She turns toward the door.", 0)
    await set_active_leaf(conv_id, mid)
    await set_workflow_config("image_gen", config or CONFIG)
    return mid


async def _attach(
    mid: int,
    *,
    seed: str = "1234",
    consumption: dict | None = None,
    webp: bool = False,
    parent_attachment_id: int | None = None,
    **generation,
) -> int:
    """One stored image_gen attachment, with the generation parameters a replay reads."""
    row = {
        "filename": "x.webp" if webp else "x.png",
        "mime": "image/webp" if webp else "image/png",
        "data": b"RIFF0000WEBPold" if webp else b"\x89PNG\r\n\x1a\nold",
        "workflow_id": "image_gen",
        "seed": seed,
        "parent_attachment_id": parent_attachment_id,
        "generation_metadata": {"style_id": "anime", "prompt": "1girl", "negative_prompt": "", **generation},
    }
    if consumption is not None:
        row["consumption_metadata"] = consumption
    return await insert_workflow_attachment_row(mid, row)


async def _trigger(client, conv_id: str, body: dict) -> list[tuple[str, dict]]:
    return _events((await client.post_checked(f"/api/conversations/{conv_id}/workflows/image_gen/trigger", json=body)).text)


async def _replay(client, conv_id: str, mid: int, aid: int, action: str = "reroll-gen", **body):
    return await client.post_checked(
        f"/api/conversations/{conv_id}/messages/{mid}/workflow-attachments/{aid}/{action}", json=body
    )


async def _sibling(mid: int, aid: int) -> dict:
    return next(row for row in await get_workflow_attachments_for_message(mid) if row["id"] != aid)


def _stub(monkeypatch, render=None, scene="1girl, standing", seen: dict | None = None):
    async def fake_compose(**kwargs):
        if seen is not None:
            seen.update(kwargs)
        return scene, "", "single_call"

    async def fake_render(adapter, request, **kwargs):
        return _image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", fake_compose)
    if render is not False:
        monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render or fake_render)


def _refine(monkeypatch, *revisions, told: list | None = None) -> None:
    reviews = iter(revisions)

    async def fake_refine(**kwargs):
        if told is not None:
            told.append(kwargs["reseeded"])
        review = next(reviews)
        return review(kwargs) if callable(review) else review

    monkeypatch.setattr("backend.workflows.image_gen.hooks.refine_scene", fake_refine)


def _labels(events) -> list[str]:
    return [data["label"] for name, data in events if name == "phase_status" and "label" in data]


def _capture_comfy(monkeypatch) -> dict:
    """Swap the ComfyUI render for one recording what it was actually targeted at."""
    captured: dict = {}

    async def capture(_self, request, *, target, progress=None):
        captured.update(checkpoint=target.model, graph_id=target.target_id, seed=request.seed, prompt=request.prompt)
        # Reported back off the target, as the real adapter reads it off the patched
        # graph -- what a render says it did is what the next replay reads.
        return _image(
            workflow_id=target.target_id,
            backend_model=target.model,
            width=target.width,
            height=target.height,
            notes=list(target.notes),
        )

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


# -- guards -------------------------------------------------------------------


@pytest.mark.parametrize(
    "body_key, message", [("bad_type", "message_id (int) required"), ("missing", "no longer part of this conversation")]
)
async def test_a_rejected_generate_still_speaks_the_stream_contract(client, monkeypatch, body_key, message):
    """A guard that answers in JSON leaves the client waiting on frames that never
    come: it sees no terminal event and re-enables its button silently."""
    conv_id = f"ig-guard-{body_key}"
    await _seed(conv_id)
    _stub(monkeypatch)
    events = await _trigger(client, conv_id, {"action": "generate", "message_id": True if body_key == "bad_type" else 999_999})

    assert [name for name, _ in events] == ["image_gen_error", "phase_status", "image_gen_done"]
    assert message in events[0][1]["message"]
    assert events[-1][1] == {"attachment_id": None}


async def test_generate_refuses_a_user_message(client, monkeypatch):
    await _seed("ig-user-msg")
    uid, _ = await add_message("ig-user-msg", "user", "draw her", 1)
    _stub(monkeypatch)

    events = await _trigger(client, "ig-user-msg", {"action": "generate", "message_id": uid})

    assert "assistant messages" in events[0][1]["message"]


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


async def test_a_render_failure_reaches_the_user_and_still_terminates(client, monkeypatch):
    mid = await _seed("ig-fail")

    async def boom(config, request, **kwargs):
        raise ValueError("ComfyUI could not complete the image")

    _stub(monkeypatch, render=boom)

    events = await _trigger(client, "ig-fail", {"action": "generate", "message_id": mid})

    assert [name for name, _ in events][-1] == "image_gen_done"
    assert events[-1][1] == {"attachment_id": None}
    assert any(name == "image_gen_error" and "could not complete" in data["message"] for name, data in events)


async def test_the_attachment_records_the_seed_the_backend_actually_drew_with(client, monkeypatch):
    """Orb picks a seed out of 2**64, and a ComfyUI graph's seed node can declare a
    smaller range and fold it. The number stored beside the image is the one a user
    types back into ComfyUI, so it has to be the one that rendered."""
    mid = await _seed("ig-seed")
    asked: dict = {}

    async def narrowing_render(adapter, request, **kwargs):
        asked["seed"] = request.seed
        return _image(seed=739759991701499)

    _stub(monkeypatch, render=narrowing_render)

    await _trigger(client, "ig-seed", {"action": "generate", "message_id": mid})

    assert (await get_workflow_attachments_for_message(mid))[0]["seed"] == "739759991701499" != str(asked["seed"])


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


# -- replay -------------------------------------------------------------------
#
# One hook, two routes, one thing they disagree about. /rehydrate owes the row the image it lost, so it renders what the row
# recorded; /reroll-gen owes the user another variant of the same subject, so it renders on the style as it stands -- otherwise
# every render setting is inert on any image already made, and only Regenerate, which rewrites the prompt too, can pick a new
# one up.

# Styles that pin targets of their own, so "what the row recorded" and "what the
# style says now" are two different answers rather than the same one twice.
PINNED_CONFIG = {
    **CONFIG,
    "styles": [
        {"id": "anime", "label": "Anime", "connection": "comfy", "workflow": "user_a", "checkpoint": "current.safetensors"},
        {
            "id": "realistic",
            "label": "Realistic",
            "connection": "comfy",
            "workflow": "user_a",
            "checkpoint": "photo.safetensors",
        },
    ],
}


async def test_reroll_renders_on_the_style_as_it_stands_now(client, monkeypatch):
    """The dice is the button for "same subject, different roll", so a checkpoint
    (or resolution, or model) changed since has to take effect. Replaying the stored
    target here is what made those pickers do nothing on an existing image."""
    mid = await _seed("ig-reroll", config=PINNED_CONFIG)
    aid = await _attach(mid, prompt="1girl, standing", workflow_id="user_a", backend_model="original.safetensors")
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-reroll", mid, aid)

    assert captured["checkpoint"] == "current.safetensors"
    assert captured["graph_id"] == "user_a"
    assert captured["seed"] != 1234, "a reroll must move the seed or it silently returns the cached image"
    # And the sibling records the render it got rather than the one its parent got: it is itself rehydratable, and a record
    # naming the parent's checkpoint would restore an image this row never made.
    assert json.loads((await _sibling(mid, aid))["generation_metadata"])["backend_model"] == "current.safetensors"


async def test_rehydrate_replays_the_stored_model_not_todays_style(client, monkeypatch):
    """The other half of the same switch: these bytes are meant to *be* the ones the
    row lost, so resolving through the style would restore a different image and
    report success."""
    mid = await _seed("ig-rehydrate", config=PINNED_CONFIG)
    aid = await _attach(mid, prompt="1girl, standing", workflow_id="user_a", backend_model="original.safetensors")
    from backend.workflows.attachment_cache import evict

    await evict(aid)
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-rehydrate", mid, aid, action="rehydrate")

    assert captured["checkpoint"] == "original.safetensors"
    assert captured["seed"] == 1234, "a rehydrate hands back the row's own seed"


async def test_a_style_override_renders_on_the_new_style_and_discloses_the_wording(client, monkeypatch):
    """Swapping style retargets the render entirely. The prompt text cannot follow --
    only the assembled string is stored, never the scene/avoid halves -- so say so."""
    mid = await _seed("ig-swap", config=PINNED_CONFIG)
    aid = await _attach(
        mid,
        workflow_id="user_a",
        backend_model="original.safetensors",
        consumption={"style_id": "anime", "style_label": "Anime"},
    )
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-swap", mid, aid, params={"style_id": "realistic"})

    assert captured["checkpoint"] == "photo.safetensors"
    sibling = await _sibling(mid, aid)
    stored = json.loads(sibling["generation_metadata"])
    assert stored["style_id"] == "realistic"
    # Rewritten, not left behind: the sibling names the target it actually ran on, so
    # its own rehydrate restores this image rather than its parent's.
    assert stored["backend_model"] == "photo.safetensors"
    cm = json.loads(sibling["consumption_metadata"])
    assert cm["style_id"] == "realistic"
    assert any("still carries the previous style" in note for note in cm["notes"])


async def test_an_override_that_keeps_the_style_still_renders_on_it(client, monkeypatch):
    mid = await _seed("ig-noswap", config=PINNED_CONFIG)
    aid = await _attach(
        mid,
        workflow_id="user_a",
        backend_model="original.safetensors",
        consumption={"style_id": "anime", "style_label": "Anime"},
    )
    captured = _capture_comfy(monkeypatch)

    await _replay(client, "ig-noswap", mid, aid, params={"style_id": "anime", "prompt": "edited"})

    assert captured["graph_id"] == "user_a"
    assert captured["checkpoint"] == "current.safetensors"
    assert captured["prompt"] == "edited"
    # No note: nothing was substituted. The style is the one the image already named,
    # and rendering on its current checkpoint is what this button is for.
    assert "notes" not in json.loads((await _sibling(mid, aid))["consumption_metadata"])


async def test_regenerate_recomposes_under_the_current_style_as_a_sibling(client, monkeypatch):
    mid = await _seed("ig-regen")
    agent_endpoint = await client.post_checked(
        "/api/endpoints", json={"url": "http://regen-agent.local", "api_key": "agent-key"}
    )
    await client.put_checked(
        f"/api/models/{agent_endpoint.json()['agent_active_model_config_id']}", json={"model_name": "regen-agent-model"}
    )
    await client.put_checked(
        "/api/settings",
        json={
            "agent_same_as_writer": False,
            "agent_endpoint_id": agent_endpoint.json()["id"],
            "agent_shared_system_prompt": "Regeneration agent system.",
        },
    )

    aid = await _attach(mid, style_id="realistic", prompt="stale")
    captured: dict = {}
    _stub(monkeypatch, scene="1girl, doorway, looking back", seen=captured)

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


async def test_regenerate_streams_the_render_phase_a_fresh_generate_shows(client, monkeypatch):
    mid = await _seed("ig-regen-phase")
    aid = await _attach(mid)

    async def render(adapter, request, *, progress=None, **kwargs):
        progress("rendering", {})
        return _image()

    _stub(monkeypatch, render=render)
    url = f"/api/conversations/ig-regen-phase/messages/{mid}/workflow-attachments/{aid}/regenerate"
    response = await client.post(url, json={}, headers={"Accept": "text/event-stream"})
    sibling_id = (await _sibling(mid, aid))["id"]
    assert _events(response.text) == [
        ("phase_status", {"label": "Rendering in ComfyUI..."}),
        ("regenerate_sibling", {"attachment_id": sibling_id}),
        ("regenerate_done", {"attachments": [sibling_id], "rejected_workflow_atts": []}),
    ]


# -- refinement ---------------------------------------------------------------


async def test_refinement_keeps_every_render_without_saving_reviews(client, monkeypatch):
    """The prompter's judgment of its own renders is unreliable, so no render it
    revised away from may be dropped: each lands as a variant the moment it exists,
    carries the review that followed it, and the last one is on show."""
    mid = await _seed("ig-refine", config={**CONFIG, "refine_turns": 2})
    _stub(monkeypatch)
    _refine(
        monkeypatch,
        *(
            lambda kw, c=critique: Revision(c, False, f"1girl, fixed {kw['render']}")
            for critique in ("hands merged", "second figure missing")
        ),
    )

    events = await _trigger(client, "ig-refine", {"action": "generate", "message_id": mid})

    landed = [data["attachment_id"] for name, data in events if name == "image_gen_render"]
    rows = {row["id"]: row for row in await get_workflow_attachments_for_message(mid)}
    assert sorted(rows) == landed and len(landed) == 3
    root, *revisions = landed
    assert rows[root]["parent_attachment_id"] is None
    assert all(rows[rid]["parent_attachment_id"] == root for rid in revisions)
    assert rows[root]["active_sibling_id"] == landed[-1]
    assert events[-1] == ("image_gen_done", {"attachment_id": landed[-1]})
    consumed = [json.loads(rows[rid]["consumption_metadata"]) for rid in landed]
    assert "fixed 2" in consumed[2]["prompt"]
    for cm in consumed:
        assert "review" not in cm and "refine" not in cm
        assert "hands merged" not in json.dumps(cm)
        assert "second figure missing" not in json.dumps(cm)
    labels = _labels(events)
    assert "Rendering revision 1... hands merged" in labels
    assert "Rendering revision 2... second figure missing" in labels
    assert not any(name in {"image_gen_review", "image_gen_refine_stage"} for name, _ in events)


async def test_review_reason_survives_queue_and_render_progress_then_clears(client, monkeypatch):
    mid = await _seed("ig-refine-events", config={**CONFIG, "refine_turns": 2})

    async def render(adapter, request, *, progress, **kwargs):
        progress("queued", {"ahead": 2})
        progress("rendering", {})
        return _image()

    _stub(monkeypatch, render=render)
    _refine(monkeypatch, Revision("hands merged", False, "1girl, fixed"), Revision("good", True))
    events = await _trigger(client, "ig-refine-events", {"action": "generate", "message_id": mid})
    labels = _labels(events)
    assert labels == [
        "Composing image prompt...",
        "Queued behind 2 renders...",
        "Rendering in ComfyUI...",
        "Reviewing render 1...",
        "Rendering revision 1... hands merged",
        "Queued behind 2 renders... hands merged",
        "Rendering in ComfyUI... hands merged",
        "Reviewing render 2...",
    ]
    assert events[-2] == ("phase_status", {"state": "done"})
    assert not any("good" in label for label in labels)
    for row in await get_workflow_attachments_for_message(mid):
        assert "hands merged" not in row["consumption_metadata"]
        assert "hands merged" not in row["generation_metadata"]


@pytest.mark.parametrize("reseed", [False, True], ids=["keep-seed", "reseed"])
async def test_a_revision_keeps_the_seed_unless_its_review_asks_for_a_new_one(client, monkeypatch, reseed):
    """A revision differs from the render it corrects by its prompt alone, unless
    the review judged the seed at fault: then the next render draws a new one, the
    next review is told so, and each row records the seed it was drawn with."""
    mid = await _seed(f"ig-refine-seed-{reseed}", config={**CONFIG, "refine_turns": 2})
    seeds: list[int] = []

    async def render(adapter, request, **kwargs):
        seeds.append(request.seed)
        return _image()

    _stub(monkeypatch, render=render)
    told: list[bool] = []
    _refine(monkeypatch, Revision("hands fused", False, "1girl, fixed", reseed=reseed), Revision("good", True), told=told)

    await _trigger(client, f"ig-refine-seed-{reseed}", {"action": "generate", "message_id": mid})

    assert (seeds[0] != seeds[1]) is reseed
    assert told == [False, reseed]
    first, second = await get_workflow_attachments_for_message(mid)
    assert [int(first["seed"]), int(second["seed"])] == seeds
    assert "review" not in json.loads(first["consumption_metadata"])


async def test_a_render_without_refinement_carries_no_run(client, monkeypatch):
    mid = await _seed("ig-refine-off")
    _stub(monkeypatch)

    events = await _trigger(client, "ig-refine-off", {"action": "generate", "message_id": mid})

    [row] = await get_workflow_attachments_for_message(mid)
    assert "refine" not in json.loads(row["consumption_metadata"])
    assert not any(name in {"image_gen_refine_stage", "image_gen_review"} for name, _ in events)


@pytest.mark.parametrize(
    ("review", "status"),
    [(None, "No usable review"), (Revision("hands merged", False), "No revised prompt")],
    ids=["no-review", "no-prompt"],
)
async def test_unusable_review_keeps_the_render_without_saving_a_reason(client, monkeypatch, review, status):
    mid = await _seed("ig-refine-stop", config={**CONFIG, "refine_turns": 2})
    _stub(monkeypatch)
    _refine(monkeypatch, review)
    events = await _trigger(client, "ig-refine-stop", {"action": "generate", "message_id": mid})
    [row] = await get_workflow_attachments_for_message(mid)
    consumption = json.loads(row["consumption_metadata"])
    assert "review" not in consumption and "refine" not in consumption
    assert "hands merged" not in row["consumption_metadata"]
    assert "notes" not in consumption
    assert any(status in data.get("label", "") for name, data in events if name == "phase_status")


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def _png_image() -> ImageResult:
    """A render that decodes: the prompter's copy of a render is re-encoded."""
    return ImageResult(image_bytes=_png(), mime="image/png", backend_info={"source": "external_comfy"})


def _compose_call(scene: str = "1girl, standing") -> dict:
    args = json.dumps({"scene": scene, "avoid": ""})
    return {"tool_calls": [{"id": "t1", "type": "function", "function": {"name": "compose_image_prompt", "arguments": args}}]}


async def test_a_prompter_that_rejects_the_earlier_chat_image_stops_generation(client, llm_mock, monkeypatch):
    """Composing blind instead would leave the setting looking as if it worked."""
    earlier = await _seed("ig-pref-fail", config={**CONFIG, "prompter_reference": True})
    await insert_workflow_attachment_row(
        earlier, {"filename": "x.png", "mime": "image/png", "data": _png(), "workflow_id": "image_gen", "seed": "1"}
    )
    uid, _ = await add_message("ig-pref-fail", "user", "Later.", 1, parent_id=earlier)
    mid, _ = await add_message("ig-pref-fail", "assistant", "She steps outside.", 2, parent_id=uid)
    await set_active_leaf("ig-pref-fail", mid)
    renders: list = []

    async def render(adapter, request, **kwargs):
        renders.append(request)
        return _png_image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render)
    llm_mock.fail("workflow", RuntimeError("400 this model does not accept image input"))

    events = await _trigger(client, "ig-pref-fail", {"action": "generate", "message_id": mid})

    [compose] = [c for c in llm_mock.captured if c["pass"] == "workflow"]
    assert compose["messages"][-1]["content"][0]["type"] == "image_url"
    assert not renders and not any(name == "image_gen_render" for name, _ in events)
    [error] = [data["message"] for name, data in events if name == "image_gen_error"]
    assert "earlier chat image failed" in error and "does not accept image input" in error
    assert events[-1] == ("image_gen_done", {"attachment_id": None})


async def test_a_review_the_provider_rejects_stops_generation_and_keeps_the_render(client, llm_mock, monkeypatch):
    mid = await _seed("ig-review-fail", config={**CONFIG, "refine_turns": 2})

    async def render(adapter, request, **kwargs):
        return _png_image()

    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render)
    llm_mock.enqueue_workflow(_compose_call())
    llm_mock.fail("workflow", RuntimeError("400 this model does not accept image input"), after=1)

    events = await _trigger(client, "ig-review-fail", {"action": "generate", "message_id": mid})

    [row] = await get_workflow_attachments_for_message(mid)
    consumption = json.loads(row["consumption_metadata"])
    assert "review" not in consumption and "refine" not in consumption
    assert "notes" not in consumption
    [error] = [data["message"] for name, data in events if name == "image_gen_error"]
    assert error.startswith("Review of render 1 failed") and "does not accept image input" in error
    assert events[-1] == ("image_gen_done", {"attachment_id": row["id"]})


async def test_a_failed_revision_ends_the_run_on_the_render_before_it(client, monkeypatch):
    mid = await _seed("ig-refine-fail", config={**CONFIG, "refine_turns": 2})
    renders = iter([_image(), ImageGenerationError("ComfyUI went away")])

    async def render(adapter, request, **kwargs):
        outcome = next(renders)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    _stub(monkeypatch, render=render)
    _refine(monkeypatch, Revision("hands merged", False, "1girl, fixed"))

    events = await _trigger(client, "ig-refine-fail", {"action": "generate", "message_id": mid})

    [row] = await get_workflow_attachments_for_message(mid)
    consumption = json.loads(row["consumption_metadata"])
    assert "review" not in consumption and "refine" not in consumption
    assert "hands merged" not in row["consumption_metadata"]
    assert any(
        data.get("label") == "Revision render failed; keeping render 1." for name, data in events if name == "phase_status"
    )


@pytest.mark.parametrize("from_sibling", [False, True])
async def test_regeneration_streams_review_reason_only_in_live_progress(client, monkeypatch, from_sibling):
    mid = await _seed("ig-regen-refine", config={**CONFIG, "refine_turns": 1})
    aid = await _attach(mid)
    target_id = await _attach(mid, parent_attachment_id=aid) if from_sibling else aid
    _stub(monkeypatch)
    _refine(monkeypatch, Revision("hands merged", False, "1girl, fixed"))
    url = f"/api/conversations/ig-regen-refine/messages/{mid}/workflow-attachments/{target_id}/regenerate"
    events = _events((await client.post_checked(url, json={}, headers={"Accept": "text/event-stream"})).text)
    assert "Rendering revision 1... hands merged" in [data["label"] for name, data in events if name == "phase_status"]
    assert not any(name in {"image_gen_review", "image_gen_refine_stage"} for name, _ in events)
    for row in await get_workflow_attachments_for_message(mid):
        if row["id"] not in {aid, target_id}:
            assert "review" not in json.loads(row["consumption_metadata"])
            assert "hands merged" not in row["consumption_metadata"]
    assert events[-1][0] == "regenerate_done"


@pytest.mark.parametrize(("action", "kept"), [("rehydrate", True), ("reroll-gen", False)])
async def test_replay_does_not_copy_legacy_review_metadata(client, monkeypatch, action, kept):
    mid = await _seed(f"ig-refine-{action}")
    placed = {"review": {"critique": "good", "done": True}, "refine": {"run": "ab12", "render": 2, "turns": 2}}
    aid = await _attach(mid, prompt="1girl", workflow_id="user_a", consumption={"style_id": "anime", **placed})
    if action == "rehydrate":
        from backend.workflows.attachment_cache import evict

        await evict(aid)
    _capture_comfy(monkeypatch)

    await _replay(client, f"ig-refine-{action}", mid, aid, action=action)

    rows = await get_workflow_attachments_for_message(mid)
    row = next(r for r in rows if r["id"] == aid) if kept else await _sibling(mid, aid)
    consumption = json.loads(row["consumption_metadata"])
    assert "review" not in consumption and "refine" not in consumption


async def test_a_user_who_paged_back_mid_run_stays_on_their_pick(client):
    mid = await _seed("ig-pick")
    render = {"workflow_id": "image_gen", "filename": "x.png", "mime": "image/png", "data": b"\x89PNGx", "seed": "1"}

    first, _ = await insert_workflow_variant(mid, render)
    second, _ = await insert_workflow_variant(mid, render, group=[first], shown=first)
    assert (await get_workflow_attachment_by_id(first))["active_sibling_id"] == second

    await set_active_sibling(first, first, expected_message_id=mid)
    third, _ = await insert_workflow_variant(mid, render, group=[first, second], shown=second)

    assert third is not None and (await get_workflow_attachment_by_id(first))["active_sibling_id"] == first


async def test_stopping_mid_write_still_saves_the_render(client):
    """Stop cancels the run; a render already being written must land anyway, or the
    user loses the image they were looking at when they pressed it."""
    mid = await _seed("ig-stop-write")
    render = {"workflow_id": "image_gen", "filename": "x.png", "mime": "image/png", "data": b"\x89PNGx", "seed": "1"}

    task = asyncio.create_task(insert_workflow_variant(mid, render))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(await get_workflow_attachments_for_message(mid)) == 1


# -- camera -------------------------------------------------------------------


async def test_pov_mode_is_global_config(client):
    """The camera rides the workflow config, like the style: one setting for every
    conversation, saved through the same config PUT the style picker uses."""
    await _seed("ig-pov-a")
    await client.put("/api/workflows/image_gen/config", json={"config": {**CONFIG, "pov_mode": "first"}})

    assert (await client.get_json("/api/workflows/image_gen/config"))["config"]["pov_mode"] == "first"

    # Junk normalizes rather than persisting an unrenderable mode.
    await client.put("/api/workflows/image_gen/config", json={"config": {**CONFIG, "pov_mode": "sideways"}})
    assert (await client.get_json("/api/workflows/image_gen/config"))["config"]["pov_mode"] == "auto"

    # The picker labels "Auto" off these two, so both must be answered whatever the machine has on disk -- a dev box with the
    # GGUF present reports ready, and an absent flag would leave the label lying either way.
    status = await client.post_json("/api/workflows/image_gen/query", json={"action": "status"})
    assert isinstance(status["classifier_ready"], bool)
    assert status["fallback_mode"] == pov.DEFAULT_POV_MODE


async def test_generate_records_the_camera_and_the_lever_that_chose_it(client, monkeypatch):
    mid = await _seed("ig-pov-meta", with_character=True)
    # The picker decides. A 'third_person' tag left in the character's appearance
    # prompt is appearance data now, not a camera override.
    await set_workflow_character_state("ig-pov-meta-char", "image_gen", {"appearance_prompt": "3D, third_person"})
    seen: dict = {}
    _stub(monkeypatch, seen=seen)
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


# -- cloud source -------------------------------------------------------------


async def test_status_under_cloud_reports_the_provider_and_never_the_key(client):
    await _seed("ig-cloud-status", config=CLOUD_CONFIG)

    status = await client.post_json("/api/workflows/image_gen/query", json={"action": "status"})

    assert status["source"] == "cloud"
    assert status["ready"] is True
    assert "grok-imagine-image" in status["detail"]
    # The provider list is the preset table projected. A configured credential
    # reaching this payload would put it on the wire on every tools-panel open.
    assert "sk-live-canary" not in json.dumps(status)


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

    answer = await client.post_json(
        "/api/workflows/image_gen/query", json={"action": "node_types", "class_types": ["CLIPTextEncode"]}
    )
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


async def test_rehydrating_a_seedless_render_discloses_that_it_is_a_fresh_image(client, monkeypatch):
    """Rehydrate promises the same bytes back. A seedless API cannot give them, so
    the row is overwritten with something the user never generated -- and billed."""
    mid, aid = await _seed_cloud_attachment("ig-cloud-rehydrate", evicted=True)
    _cloud_render(monkeypatch)

    await _replay(client, "ig-cloud-rehydrate", mid, aid, action="rehydrate")

    row = next(r for r in await get_workflow_attachments_for_message(mid) if r["id"] == aid)
    assert any("takes no seed" in note for note in json.loads(row["consumption_metadata"])["notes"])


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


async def test_rerolling_a_comfy_image_under_cloud_discloses_the_source_change(client, monkeypatch):
    mid = await _seed("ig-cross-source", config=CLOUD_CONFIG)
    aid = await _attach(mid, source="external_comfy", workflow_id="user_a")
    _cloud_render(monkeypatch)

    await _replay(client, "ig-cross-source", mid, aid)

    notes = json.loads((await _sibling(mid, aid))["consumption_metadata"])["notes"]
    assert any("External ComfyUI" in note and "xAI" in note for note in notes), notes


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
            200, json={"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}], "usage": {"cost_in_usd_ticks": 900}}
        )

    monkeypatch.setattr(
        openai_image.OpenAICompatibleImageAdapter,
        "_client",
        lambda self, timeout: OpenAIImageClient(
            "https://api.x.ai/v1", "sk-live-canary", label=self.label, transport=httpx.MockTransport(handler)
        ),
    )

    seen: dict = {}
    _stub(monkeypatch, render=False, scene="1girl, standing by the window", seen=seen)  # only the prompter is stubbed

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
    # `n` joins them: one image is what the provider returns unasked, and asking cost
    # `gemini-3-pro-image` the ability to render at all.
    for absent in ("size", "negative_prompt", "seed", "n"):
        assert absent not in submitted["body"]

    rows = await get_workflow_attachments_for_message(mid)
    metadata = json.loads(rows[0]["generation_metadata"])
    assert metadata["source"] == "cloud"
    assert metadata["backend_model"] == "grok-imagine-image"
    # Real pixels, probed off what came back -- not the 1536x1024 that was asked for.
    assert (metadata["width"], metadata["height"]) == (1344, 768)
    assert metadata["seed_honored"] is False
    # The seed is still stored: rehydrate 409s on a null one, which would make every cloud image permanently unrehydratable.
    assert rows[0]["seed"]

    consumption = json.loads(rows[0]["consumption_metadata"])
    assert consumption["source"] == "xAI (Grok)"
    assert consumption["cost"] == {"provider": "xai", "unit": "usd_ticks", "value": 900}
    # The negative is recorded even though it was never sent, so replaying this image on ComfyUI later is still correct.
    assert "negative_prompt" in consumption
