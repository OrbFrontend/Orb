"""Reroll and rehydrate must reproduce the image the row records, not the style.

The failure this guards is silent: resolving replay through the style renders an
old attachment on whatever checkpoint that style points at *today*, and for
rehydrate -- which promises to restore evicted bytes -- that overwrites the row
with a different image and reports success.
"""

from __future__ import annotations

import pytest

from backend.workflows.image_gen import hooks
from backend.workflows.image_gen.config import normalize_config
from backend.workflows.image_gen.engine import (
    ImageGenerationError,
    resolve_render_target,
)

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


def test_a_fresh_render_follows_the_style():
    # The style pins no workflow and external mode ships no default graph, so the
    # target graph is empty; the adapter turns that into an "assign a workflow" error.
    target = resolve_render_target(_config(), "anime")
    assert (target.graph_id, target.checkpoint, target.notes) == ("", "current.safetensors", ())


def test_replay_prefers_what_the_stored_image_recorded():
    config = _config(user_graphs=[{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}])
    target = resolve_render_target(config, "anime", {"workflow_id": "user_a", "backend_model": "old.safetensors"})
    assert (target.graph_id, target.checkpoint) == ("user_a", "old.safetensors")
    assert target.notes == ()


def test_replay_of_a_deleted_graph_degrades_with_disclosure():
    target = resolve_render_target(_config(), "anime", {"workflow_id": "user_gone", "backend_model": "old.safetensors"})
    # The style has no workflow to fall back to, so the target is empty and the
    # note discloses both the missing graph and the unconfigured style.
    assert target.graph_id == ""
    assert target.checkpoint == "old.safetensors"
    assert len(target.notes) == 1
    assert "user_gone" in target.notes[0]


def test_a_user_graph_replay_without_a_recorded_model_falls_through_to_the_style():
    """`backend_model` is null when the original ran a graph carrying its own
    loaders, so there is no pin to restore -- inventing one would be worse."""
    config = _config(user_graphs=[{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}])
    target = resolve_render_target(config, "anime", {"workflow_id": "user_a", "backend_model": None})
    assert (target.graph_id, target.checkpoint) == ("user_a", "current.safetensors")


# ── reference images on reroll ───────────────────────────────────────────────

EDIT_GRAPH = {**GRAPH, "r": {"class_type": "LoadImage", "inputs": {"image": "exported.png"}}}
EDIT_SLOTS = {**SLOTS, "references": [{"slot": ["r", "image"], "source": "character", "label": "Load Image (#r)"}]}


class _RerollCtx:
    def __init__(self, prior_style: str):
        self.prior_consumption_metadata = {"style_id": prior_style}


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
async def test_a_style_swap_on_reroll_drops_the_recorded_references():
    """They name node ids in the OLD graph, so they cannot be replayed onto a
    different one -- and RerollGenCtx carries no history to re-resolve from."""
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
    assert "references" not in params and "workflow_id" not in params


@pytest.mark.asyncio
@pytest.mark.usefixtures("_edit_config")
async def test_rerolling_onto_a_reference_style_is_refused_with_a_reason():
    """Submitting anyway would ship the new graph's exporter filenames, which
    fails at ComfyUI with nothing the user can act on."""
    params = {"prompt": "p", "negative_prompt": "", "style_id": "edit", "workflow_id": "user_other"}

    with pytest.raises(ImageGenerationError, match="reference images"):
        await hooks.reroll_gen(_RerollCtx("plain"), params, "1")
