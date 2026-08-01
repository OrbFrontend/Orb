from __future__ import annotations

from backend.workflows.image_gen.config import (
    DEFAULT_PROMPT_FORMAT,
    MAX_REFERENCE_IMAGE_B64,
    MAX_REFERENCE_SLOTS,
    MAX_USER_GRAPHS,
    PROMPT_FORMATS,
    normalize_config,
    normalize_profile,
    resolve_style,
)
from backend.workflows.image_gen.hooks import fold_seed


def test_a_style_id_does_not_change_empty_prompt_fields():
    cfg = normalize_config(
        {"external_comfy": {"styles": [{"id": "anime", "label": "Anything", "prompt": "", "negative_prompt": ""}]}}
    )
    style = resolve_style(cfg, "anime")
    assert style["prompt_format"] == DEFAULT_PROMPT_FORMAT
    assert style["prompt"] == ""
    assert style["negative_prompt"] == ""


def test_style_prompt_format_is_explicit_and_limited_to_three_choices():
    styles = [{"id": prompt_format, "prompt_format": prompt_format} for prompt_format in PROMPT_FORMATS]
    styles.append({"id": "invalid", "prompt_format": "checkpoint-dependent"})
    cfg = normalize_config({"external_comfy": {"styles": styles}})

    assert [resolve_style(cfg, prompt_format)["prompt_format"] for prompt_format in PROMPT_FORMATS] == list(PROMPT_FORMATS)
    assert resolve_style(cfg, "invalid")["prompt_format"] == DEFAULT_PROMPT_FORMAT


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
    # No global default to inherit, and no shipped core graph to fall back on: an
    # empty checkpoint stays empty and an empty workflow stays empty (unconfigured).
    plain = resolve_style(cfg, "plain")
    assert (plain["checkpoint"], plain["workflow"]) == ("", "")


def test_a_style_naming_the_removed_core_graph_migrates_to_unconfigured():
    # "external_core" was the shipped default; it no longer exists. A stored config
    # that still names it must read as "no workflow", not as a dangling reference.
    cfg = normalize_config({"external_comfy": {"styles": [{"id": "legacy", "label": "Legacy", "workflow": "external_core"}]}})
    assert resolve_style(cfg, "legacy")["workflow"] == ""


def test_default_style_falls_back_when_it_no_longer_resolves():
    cfg = normalize_config({"default_style": "deleted", "external_comfy": {"styles": [{"id": "only", "label": "Only"}]}})
    assert cfg["default_style"] == "only"


# ── reference slots ──────────────────────────────────────────────────────────

_BASE_SLOTS = {"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]}


def _stored(user_graph: dict) -> dict:
    return normalize_config({"external_comfy": {"user_graphs": [user_graph]}})["external_comfy"]["user_graphs"][0]


def test_reference_slots_survive_normalization():
    stored = _stored(
        _user_graph(
            slots={
                **_BASE_SLOTS,
                "references": [
                    {"slot": ["72", "image"], "source": "previous_or_character", "label": "Load Image (#72)"},
                    {"slot": [90, "image"], "source": "character"},
                ],
            }
        )
    )
    assert stored["slots"]["references"][0] == {
        "slot": ["72", "image"],
        "source": "previous_or_character",
        "label": "Load Image (#72)",
    }
    # A numeric node id normalizes to a string, and a missing label gets a usable one.
    assert stored["slots"]["references"][1]["slot"] == ["90", "image"]
    assert stored["slots"]["references"][1]["label"]


def test_an_unknown_reference_source_is_dropped_not_stored():
    stored = _stored(
        _user_graph(
            slots={
                **_BASE_SLOTS,
                "references": [
                    {"slot": ["72", "image"], "source": "whatever_the_user_typed"},
                    {"slot": ["90", "image"], "source": "character"},
                ],
            }
        )
    )
    assert [r["slot"] for r in stored["slots"]["references"]] == [["90", "image"]]


def test_reference_slots_are_capped():
    references = [{"slot": [str(i), "image"], "source": "character"} for i in range(MAX_REFERENCE_SLOTS + 3)]
    stored = _stored(_user_graph(slots={**_BASE_SLOTS, "references": references}))
    assert len(stored["slots"]["references"]) == MAX_REFERENCE_SLOTS


def test_a_graph_with_no_references_round_trips_unchanged():
    """A plain text-to-image graph must normalize exactly as it did before
    references existed -- no empty key introduced into its slot map."""
    stored = _stored(_user_graph(slots=dict(_BASE_SLOTS)))
    assert stored["slots"] == _BASE_SLOTS
    assert "references" not in stored["slots"]


def test_is_changed_is_stripped_from_every_node_at_import():
    """ComfyUI's API export embeds `is_changed` -- for a LoadImage node, a hash of
    the file on the exporter's disk. IsChangedCache returns a client-supplied value
    verbatim instead of computing the real one, so a stored hash makes ComfyUI miss
    a file whose *contents* changed under an unchanged path and hand back the
    previously decoded image. Machine-local state about another machine's disk has
    no business in a stored graph regardless."""
    graph = _graph()
    graph["0"]["is_changed"] = ["b80d1d64deadbeef"]
    graph["s"]["is_changed"] = ["another"]
    stored = _stored({"id": "user_a", "label": "a", "graph": graph, "slots": dict(_BASE_SLOTS)})
    assert all("is_changed" not in node for node in stored["graph"].values())
    # Only the machine-local key goes; the node itself is intact.
    assert stored["graph"]["0"]["inputs"]["text"]


# ── per-character reference image ────────────────────────────────────────────


def test_a_character_reference_image_needs_both_halves():
    profile = normalize_profile({"reference_image_b64": "aGk=", "reference_mime": "image/png"})
    assert (profile["reference_image_b64"], profile["reference_mime"]) == ("aGk=", "image/png")

    # A payload Orb cannot tell ComfyUI how to read is not a reference.
    assert normalize_profile({"reference_image_b64": "aGk=", "reference_mime": "text/plain"})["reference_image_b64"] == ""
    assert normalize_profile({"reference_image_b64": "aGk="})["reference_image_b64"] == ""
    # ...and a mime with no bytes is not a half-set field either.
    assert normalize_profile({"reference_mime": "image/png"})["reference_mime"] == ""


def test_an_oversized_reference_image_is_dropped_rather_than_truncated():
    """Half a base64 payload is not a smaller image, it is a corrupt one -- and
    this profile is read on every generate."""
    oversized = "A" * (MAX_REFERENCE_IMAGE_B64 + 1)
    profile = normalize_profile({"reference_image_b64": oversized, "reference_mime": "image/png"})
    assert (profile["reference_image_b64"], profile["reference_mime"]) == ("", "")
