"""Reroll and rehydrate must reproduce the image the row records, not the style.

The failure this guards is silent: resolving replay through the style renders an
old attachment on whatever checkpoint that style points at *today*, and for
rehydrate -- which promises to restore evicted bytes -- that overwrites the row
with a different image and reports success.
"""

from __future__ import annotations

import base64

import pytest

from backend.workflows.image_gen import hooks
from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.engine import ImageGenerationError, get_adapter

GRAPH = {
    "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
    "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
}
SLOTS = {"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]}


def _config(default_style: str = "anime", **external) -> dict:
    base = {
        "styles": [{"id": "anime", "label": "Anime", "checkpoint": "current.safetensors"}],
    }
    base.update(external)
    return normalize_config({"default_style": default_style, "external_comfy": base})


def _target(config: dict, style_id: str, replay: dict | None = None):
    """What the render path resolves: the style's adapter, asked about that style."""
    style = resolve_style(config, style_id)
    return get_adapter(config, style).resolve_target(replay)


def test_a_fresh_render_follows_the_style():
    # The style pins no workflow and external mode ships no default graph, so the
    # target graph is empty; the adapter turns that into an "assign a workflow" error.
    target = _target(_config(), "anime")
    assert (target.source, target.target_id, target.model, target.notes) == ("external_comfy", "", "current.safetensors", ())
    # No graph, so no mapped size slots: the workflow decides, exactly as before the
    # slot existed. Orb pins a resolution only where it can actually write one.
    assert (target.supports_dimensions, target.width, target.height) == (False, None, None)


def test_a_graph_mapping_size_slots_takes_the_styles_resolution():
    sized = {
        "id": "user_sized",
        "label": "Sized",
        "graph": {**GRAPH, "l": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}}},
        "slots": {**SLOTS, "width": ["l", "width"], "height": ["l", "height"]},
    }
    config = _config(
        user_graphs=[sized, {"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}],
        styles=[
            {"id": "anime", "label": "Anime", "workflow": "user_sized", "width": 1024, "height": 1536},
            {"id": "own", "label": "Own", "workflow": "user_a", "width": 1024, "height": 1536},
        ],
    )
    sized_target = _target(config, "anime")
    assert (sized_target.supports_dimensions, sized_target.width, sized_target.height) == (True, 1024, 1536)
    assert sized_target.notes == ()

    # The same style setting against a graph that maps nothing: inert, and disclosed
    # rather than left to be noticed, because the picker still shows the resolution.
    own = _target(config, "own")
    assert (own.supports_dimensions, own.width, own.height) == (False, None, None)
    assert any("decides its own output size" in note for note in own.notes)


def test_a_replay_pins_the_resolution_it_was_generated_at():
    """The substitution rehydrate exists to avoid, now reachable on ComfyUI too:
    once Orb can write the size, reading today's picker hands a rehydrate an image of
    a different shape than the one it promised to restore."""
    sized = {
        "id": "user_sized",
        "label": "Sized",
        "graph": {**GRAPH, "l": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}}},
        "slots": {**SLOTS, "width": ["l", "width"], "height": ["l", "height"]},
    }
    config = _config(
        user_graphs=[sized],
        styles=[{"id": "anime", "label": "Anime", "workflow": "user_sized", "width": 1536, "height": 1024}],
    )
    target = _target(config, "anime", {"workflow_id": "user_sized", "width": 1024, "height": 1024})
    assert (target.width, target.height) == (1024, 1024)


@pytest.mark.parametrize(
    ("replay", "expected"),
    [
        ({"workflow_id": "user_a", "backend_model": "old.safetensors"}, ("user_a", "old.safetensors")),
        # `backend_model` is null when the original ran a graph carrying its own
        # loaders, so there is no pin to restore -- inventing one would be worse.
        ({"workflow_id": "user_a", "backend_model": None}, ("user_a", "current.safetensors")),
    ],
    ids=["recorded pins win", "no recorded model falls through to the style"],
)
def test_replay_prefers_what_the_stored_image_recorded(replay, expected):
    config = _config(user_graphs=[{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}])
    target = _target(config, "anime", replay)
    assert (target.target_id, target.model) == expected
    assert target.notes == ()


def test_replay_of_a_deleted_graph_degrades_with_disclosure():
    target = _target(_config(), "anime", {"workflow_id": "user_gone", "backend_model": "old.safetensors"})
    # The style has no workflow to fall back to, so the target is empty and the
    # note discloses both the missing graph and the unconfigured style.
    assert (target.target_id, target.model) == ("", "old.safetensors")
    assert len(target.notes) == 1
    assert "user_gone" in target.notes[0]


# ── reference images on reroll ───────────────────────────────────────────────

EDIT_GRAPH = {**GRAPH, "r": {"class_type": "LoadImage", "inputs": {"image": "exported.png"}}}
EDIT_SLOTS = {**SLOTS, "references": [{"slot": ["r", "image"], "source": "character", "label": "Load Image (#r)"}]}


class _RerollCtx:
    def __init__(self, prior_style: str, *, stored_seed: str = "1234"):
        self.prior_consumption_metadata = {"style_id": prior_style}
        # The rehydrate discriminator reads this: same seed back means rehydrate,
        # a freshly minted one means reroll.
        self.original_attachment = {"seed": stored_seed}


@pytest.fixture
def _edit_config(monkeypatch):
    config = _config(
        default_style="edit",
        user_graphs=[{"id": "user_edit", "label": "Edit", "graph": EDIT_GRAPH, "slots": EDIT_SLOTS}],
        styles=[{"id": "edit", "label": "Edit", "workflow": "user_edit"}, {"id": "plain", "label": "Plain"}],
    )

    async def get_config(_workflow_id):
        return config

    monkeypatch.setattr(hooks, "get_workflow_config", get_config)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_edit_config")
async def test_a_style_swap_on_reroll_drops_the_stale_graph_pins():
    """`workflow_id` and `backend_model` name things the OLD style owned, so the
    new style must resolve its own. `references` is not one of them -- it records
    an *origin*, which re-fetches with no history and no graph."""
    # The override swapped this reroll from the edit style onto a plain one.
    params = {
        "prompt": "a quiet room",
        "negative_prompt": "",
        "style_id": "plain",
        "workflow_id": "user_edit",
        "references": [{"slot": ["r", "image"], "source": "character", "origin": "character:card-1"}],
    }

    # The plain style pins no workflow, so the render dies on the normal "assign a
    # workflow" path -- but only after the stale pins are gone from `params`, which
    # is what the persisted sibling records.
    with pytest.raises(ImageGenerationError, match="Import a ComfyUI workflow"):
        await hooks.reroll_gen(_RerollCtx("edit"), params, "1")
    assert "workflow_id" not in params and "backend_model" not in params


@pytest.mark.asyncio
@pytest.mark.usefixtures("_edit_config")
async def test_rerolling_onto_a_style_needing_an_unrecorded_reference_is_refused():
    """Submitting anyway would ship the new graph's exporter filenames, which
    fails at ComfyUI with nothing the user can act on. The refusal now fires on
    what is actually unreplayable -- a required slot with no recorded origin to
    fill it -- rather than on any style change that touches references at all."""
    params = {"prompt": "p", "negative_prompt": "", "style_id": "edit", "workflow_id": "user_other"}

    with pytest.raises(ImageGenerationError, match="needs a reference image the stored image did not record"):
        await hooks.reroll_gen(_RerollCtx("plain"), params, "1")


# ── routing, when the replayed style is not the default one ──────────────────


@pytest.mark.asyncio
async def test_a_replay_routes_on_its_own_style_not_the_configs_default(monkeypatch):
    """The regression this plan is fixing. `normalize_config` derives `source` from
    the *default* style, and `/rehydrate` calls the hook with the attachment's stored
    `style_id` -- whatever the image was originally made with. Routing on `source`
    therefore handed a ComfyUI-linked style to the cloud adapter, which answered
    "Choose a model for xAI" about a style holding a perfectly good checkpoint.

    `/reroll-gen` never showed it because the widget overwrites `style_id` with the
    default style on every reroll; rehydrate does not.
    """
    from backend.workflows.image_gen.engine.contracts import ImageResult

    config = normalize_config(
        {
            "default_style": "remote",
            "styles": [
                {"id": "remote", "connection": "xai"},
                {"id": "local", "connection": "comfy", "workflow": "user_a", "checkpoint": "anime.safetensors"},
            ],
            "external_comfy": {"user_graphs": [{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}]},
            "cloud": {"providers": {"xai": {"api_key": "k"}}},
        }
    )
    assert config["source"] == "cloud", "precondition: the default style routes to the cloud"

    async def get_config(_workflow_id):
        return config

    captured: dict = {}

    async def fake_generate(_adapter, request, *, target=None, progress=None):
        captured["target"] = target
        return ImageResult(image_bytes=b"rendered", mime="image/png", backend_info={"notes": []})

    monkeypatch.setattr(hooks, "get_workflow_config", get_config)
    monkeypatch.setattr(hooks, "resolve_and_generate", fake_generate)

    params = {"prompt": "a quiet room", "negative_prompt": "", "style_id": "local", "source": "external_comfy"}
    _, consumption = await hooks.reroll_gen(_RerollCtx("local"), params, "1")

    assert captured["target"].source == "external_comfy"
    assert (captured["target"].target_id, captured["target"].model) == ("user_a", "anime.safetensors")
    assert consumption["source"] == "External ComfyUI"
    # And nothing claims a backend change, because there was none.
    assert not any("re-rendered on" in note for note in consumption.get("notes", []))


# ── reference images on a cloud reroll ───────────────────────────────────────
#
# The cloud slot is synthetic and constant, so every question the ComfyUI cases
# above answer about node ids has a different answer here.


def _cloud_config(reference_source: str, styles=None) -> dict:
    return normalize_config(
        {
            "source": "cloud",
            "default_style": "anime",
            "styles": styles or [{"id": "anime", "label": "Anime", "connection": "xai"}],
            "cloud": {
                "provider": "xai",
                "reference_source": reference_source,
                "providers": {
                    "xai": {
                        "api_key": "sk-test",
                        "model": "grok-imagine-image",
                        "reference_source": reference_source,
                    }
                },
            },
        }
    )


@pytest.fixture
def _cloud_reroll(monkeypatch):
    """Configure a cloud reroll and capture the request that reaches the adapter."""
    import io

    from PIL import Image

    from backend.workflows.image_gen import references as refs
    from backend.workflows.image_gen.engine.contracts import ImageResult

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (10, 20, 30)).save(buf, format="WEBP")
    stored = buf.getvalue()

    async def by_id(_att_id):
        return {"id": 10, "mime_type": "image/webp", "data_b64": base64.b64encode(stored).decode()}

    monkeypatch.setattr(refs, "get_workflow_attachment_by_id", by_id)
    captured: dict = {}

    async def fake_generate(_adapter, request, *, target=None, progress=None):
        captured["request"] = request
        captured["target"] = target
        return ImageResult(image_bytes=b"rendered", mime="image/webp", backend_info={"notes": []})

    monkeypatch.setattr(hooks, "resolve_and_generate", fake_generate)

    def _configure(config: dict):
        async def get_config(_workflow_id):
            return config

        monkeypatch.setattr(hooks, "get_workflow_config", get_config)
        return captured

    return _configure


RECORDED_CLOUD = [{"slot": ["cloud", "image_0"], "source": "previous", "origin": "attachment:10", "digest": "x"}]


@pytest.mark.asyncio
async def test_a_cloud_reroll_with_references_off_drops_them_and_says_so(_cloud_reroll):
    """Submitting them anyway is what sent a stored WebP into an edits endpoint
    that had declared PNG/JPEG -- the target's slot list is empty, so there was no
    policy to convert under and the ComfyUI defaults applied."""
    captured = _cloud_reroll(_cloud_config(""))
    params = {"prompt": "p", "negative_prompt": "", "style_id": "anime", "references": list(RECORDED_CLOUD)}

    _, consumption = await hooks.reroll_gen(_RerollCtx("anime"), params, "1")

    assert captured["request"].references == ()
    assert any("does not take reference images" in note for note in consumption["notes"])
    # And the sibling records what was actually sent, not what the parent recorded.
    assert params["references"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("style_id", ["anime", "realistic"], ids=["same style", "style changed"])
async def test_a_cloud_reroll_converts_the_reference_to_what_the_provider_takes(_cloud_reroll, style_id):
    """A style change carries the reference over: refusing it was right only for
    ComfyUI's recorded node ids, and a cloud reference is an origin against a
    constant synthetic slot, so it replays as it stands."""
    styles = [
        {"id": "anime", "label": "Anime", "connection": "xai"},
        {"id": "realistic", "label": "Realistic", "connection": "xai"},
    ]
    captured = _cloud_reroll(_cloud_config("previous", styles=styles))
    params = {"prompt": "p", "negative_prompt": "", "style_id": style_id, "references": list(RECORDED_CLOUD)}

    await hooks.reroll_gen(_RerollCtx("anime"), params, "1")

    (reference,) = captured["request"].references
    assert reference.mime in ("image/png", "image/jpeg")
    assert reference.slot == ("cloud", "image_0")
