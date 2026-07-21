"""Reroll and rehydrate must reproduce the image the row records, not the style.

The failure this guards is silent: resolving replay through the style renders an
old attachment on whatever checkpoint that style points at *today*, and for
rehydrate -- which promises to restore evicted bytes -- that overwrites the row
with a different image and reports success.
"""

from __future__ import annotations

from backend.workflows.image_gen.config import normalize_config
from backend.workflows.image_gen.engine import resolve_render_target

GRAPH = {
    "0": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
    "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
}
SLOTS = {"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]}


def _config(**external) -> dict:
    base = {
        "styles": [{"id": "anime", "label": "Anime", "checkpoint": "current.safetensors"}],
    }
    base.update(external)
    return normalize_config({"default_style": "anime", "external_comfy": base})


def test_a_fresh_render_follows_the_style():
    target = resolve_render_target(_config(), "anime")
    assert (target.graph_id, target.checkpoint, target.notes) == ("external_core", "current.safetensors", ())


def test_replay_prefers_what_the_stored_image_recorded():
    config = _config(user_graphs=[{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}])
    target = resolve_render_target(config, "anime", {"workflow_id": "user_a", "backend_model": "old.safetensors"})
    assert (target.graph_id, target.checkpoint) == ("user_a", "old.safetensors")
    assert target.notes == ()


def test_replay_of_a_deleted_graph_degrades_with_disclosure():
    target = resolve_render_target(_config(), "anime", {"workflow_id": "user_gone", "backend_model": "old.safetensors"})
    assert target.graph_id == "external_core"
    assert target.checkpoint == "old.safetensors"
    assert len(target.notes) == 1
    assert "user_gone" in target.notes[0]


def test_a_user_graph_replay_without_a_recorded_model_falls_through_to_the_style():
    """`backend_model` is null when the original ran a graph carrying its own
    loaders, so there is no pin to restore -- inventing one would be worse."""
    config = _config(user_graphs=[{"id": "user_a", "label": "Mine", "graph": GRAPH, "slots": SLOTS}])
    target = resolve_render_target(config, "anime", {"workflow_id": "user_a", "backend_model": None})
    assert (target.graph_id, target.checkpoint) == ("user_a", "current.safetensors")
