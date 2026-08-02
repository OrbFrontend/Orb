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
    """What the render path resolves: the config's adapter, asked about one style."""
    return get_adapter(config).resolve_target(resolve_style(config, style_id), replay)


def test_a_fresh_render_follows_the_style():
    # The style pins no workflow and external mode ships no default graph, so the
    # target graph is empty; the adapter turns that into an "assign a workflow" error.
    target = _target(_config(), "anime")
    assert (target.source, target.target_id, target.model, target.notes) == ("external_comfy", "", "current.safetensors", ())
    # The graph carries its own latent size, so Orb never pins one.
    assert (target.supports_dimensions, target.width, target.height) == (False, None, None)


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

    async def fake_generate(_config, request, *, target=None, progress=None):
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
