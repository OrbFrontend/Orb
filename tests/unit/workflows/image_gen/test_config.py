from __future__ import annotations

from backend.workflows.image_gen.config import (
    MAX_USER_GRAPHS,
    normalize_config,
    resolve_style,
)
from backend.workflows.image_gen.hooks import fold_seed


def test_a_style_id_does_not_change_empty_prompt_fields():
    cfg = normalize_config(
        {"external_comfy": {"styles": [{"id": "anime", "label": "Anything", "prompt": "", "negative_prompt": ""}]}}
    )
    style = resolve_style(cfg, "anime")
    assert style["prompt"] == ""
    assert style["negative_prompt"] == ""


def test_config_rejects_credentials_in_url_and_bounds_timeout():
    cfg = normalize_config(
        {
            "timeout_seconds": "9999",
            "external_comfy": {"api_url": "http://user:secret@example.test:8188"},
        }
    )
    assert cfg["external_comfy"]["api_url"] == "http://127.0.0.1:8188"
    assert cfg["timeout_seconds"] == 900.0


def test_prompter_reasoning_is_an_explicit_boolean_defaulting_off():
    assert normalize_config({})["prompter_reasoning"] is False
    assert normalize_config({"prompter_reasoning": False})["prompter_reasoning"] is False
    assert normalize_config({"prompter_reasoning": True})["prompter_reasoning"] is True
    assert normalize_config({"prompter_reasoning": "true"})["prompter_reasoning"] is False


def test_seed_fold_round_trips_decimal_and_framework_hex():
    assert fold_seed("18446744073709551615") == 2**64 - 1
    assert fold_seed("ffffffffffffffffffffffffffffffff") == 2**64 - 1
    assert fold_seed("18446744073709551615") == fold_seed(fold_seed("18446744073709551615"))


def _graph(node_count: int = 1) -> dict:
    return {str(i): {"class_type": "CLIPTextEncode", "inputs": {"text": "x" * 64}} for i in range(node_count)} | {
        "s": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "o": {"class_type": "SaveImage", "inputs": {"images": ["0", 0]}},
    }


def _user_graph(gid: str = "user_a", *, node_count: int = 1, slots: dict | None = None) -> dict:
    return {
        "id": gid,
        "label": gid,
        "graph": _graph(node_count),
        "slots": slots
        if slots is not None
        else {"positive": ["0", "text"], "negative": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]},
    }


def test_user_graphs_are_bounded_by_size_and_count():
    """Oversized or over-count imports are dropped, not stored and half-honoured."""
    oversized = normalize_config({"external_comfy": {"user_graphs": [_user_graph(node_count=6_000)]}})
    assert oversized["external_comfy"]["user_graphs"] == []

    many = normalize_config({"external_comfy": {"user_graphs": [_user_graph(f"user_{i}") for i in range(MAX_USER_GRAPHS + 5)]}})
    assert len(many["external_comfy"]["user_graphs"]) == MAX_USER_GRAPHS


def test_a_user_graph_needs_positive_seed_and_output_but_not_negative():
    without_negative = _user_graph(slots={"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]})
    cfg = normalize_config({"external_comfy": {"user_graphs": [without_negative]}})
    assert [g["id"] for g in cfg["external_comfy"]["user_graphs"]] == ["user_a"]
    assert "negative" not in cfg["external_comfy"]["user_graphs"][0]["slots"]

    without_seed = _user_graph(slots={"positive": ["0", "text"], "output": ["o", "images"]})
    assert normalize_config({"external_comfy": {"user_graphs": [without_seed]}})["external_comfy"]["user_graphs"] == []


def test_a_user_graph_keeps_an_optional_checkpoint_slot():
    # The model-override slot must survive normalization, or the user's Orb model
    # selection would be silently dropped on save and never reach the graph.
    with_model = _user_graph(
        slots={"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"], "checkpoint": ["m", "unet_name"]}
    )
    cfg = normalize_config({"external_comfy": {"user_graphs": [with_model]}})
    assert cfg["external_comfy"]["user_graphs"][0]["slots"]["checkpoint"] == ["m", "unet_name"]


def test_each_style_carries_its_own_checkpoint_and_workflow():
    cfg = normalize_config(
        {
            "external_comfy": {
                "user_graphs": [_user_graph("user_a")],
                "styles": [
                    {"id": "pinned", "label": "Pinned", "checkpoint": "pinned.safetensors", "workflow": "user_a"},
                    {"id": "plain", "label": "Plain"},
                ],
            }
        }
    )
    pinned = resolve_style(cfg, "pinned")
    assert (pinned["checkpoint"], pinned["workflow"]) == ("pinned.safetensors", "user_a")
    # No global default to inherit: an empty checkpoint stays empty, an empty
    # workflow resolves to the shipped core graph.
    plain = resolve_style(cfg, "plain")
    assert (plain["checkpoint"], plain["workflow"]) == ("", "external_core")


def test_default_style_falls_back_when_it_no_longer_resolves():
    cfg = normalize_config({"default_style": "deleted", "external_comfy": {"styles": [{"id": "only", "label": "Only"}]}})
    assert cfg["default_style"] == "only"
