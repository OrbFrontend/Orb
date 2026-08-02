from __future__ import annotations

import pytest

from backend.workflows.image_gen.config import (
    DEFAULT_PROMPT_FORMAT,
    MAX_CLOUD_PROVIDERS,
    MAX_REFERENCE_IMAGE_B64,
    MAX_REFERENCE_SLOTS,
    MAX_USER_GRAPHS,
    PROMPT_FORMATS,
    normalize_config,
    normalize_profile,
    resolve_style,
)
from backend.workflows.image_gen.hooks import fold_seed

# ── styles ───────────────────────────────────────────────────────────────────


def test_style_prompt_format_is_explicit_and_limited_to_three_choices():
    styles = [{"id": fmt, "prompt_format": fmt} for fmt in PROMPT_FORMATS]
    styles += [{"id": "invalid", "prompt_format": "checkpoint-dependent"}, {"id": "unset", "prompt": ""}]
    cfg = normalize_config({"external_comfy": {"styles": styles}})

    assert [resolve_style(cfg, fmt)["prompt_format"] for fmt in PROMPT_FORMATS] == list(PROMPT_FORMATS)
    # Anything unrecognized, or absent, reads as the default rather than as itself.
    assert resolve_style(cfg, "invalid")["prompt_format"] == DEFAULT_PROMPT_FORMAT
    assert resolve_style(cfg, "unset")["prompt_format"] == DEFAULT_PROMPT_FORMAT
    assert resolve_style(cfg, "unset")["prompt"] == ""


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
    # No global default to inherit and no shipped core graph to fall back on, so an
    # empty pin stays empty (unconfigured) rather than being substituted.
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


def test_styles_hoist_out_of_external_comfy_and_a_current_list_wins():
    """Styles are shared across sources now, so they live at the top level.

    The normalizer runs on GET, on PUT, and on the value read back, so a legacy
    config hoists on first read and persists hoisted on first write -- there is no
    DB migration to write, and this is the only thing that makes that true.
    """
    legacy = {"external_comfy": {"api_key": "k", "styles": [{"id": "legacy", "label": "Legacy", "prompt": "p"}]}}
    cfg = normalize_config(legacy)

    assert [s["id"] for s in cfg["styles"]] == ["legacy"]
    assert "styles" not in cfg["external_comfy"]
    assert cfg["external_comfy"]["api_key"] == "k"  # the rest of the block is untouched
    assert normalize_config(cfg)["styles"] == cfg["styles"]  # and an already-hoisted config is a fixed point

    current = normalize_config({"styles": [{"id": "current"}], "external_comfy": {"styles": [{"id": "stale"}]}})
    assert [s["id"] for s in current["styles"]] == ["current"]


# ── global fields ────────────────────────────────────────────────────────────


def test_config_rejects_credentials_in_url_and_bounds_timeout():
    cfg = normalize_config({"timeout_seconds": "9999", "external_comfy": {"api_url": "http://user:secret@example.test:8188"}})
    assert cfg["external_comfy"]["api_url"] == "http://127.0.0.1:8188"
    assert cfg["timeout_seconds"] == 900.0


def test_prompter_reasoning_is_an_explicit_boolean_defaulting_off():
    assert normalize_config({})["prompter_reasoning"] is False
    assert normalize_config({"prompter_reasoning": True})["prompter_reasoning"] is True
    assert normalize_config({"prompter_reasoning": "true"})["prompter_reasoning"] is False


def test_source_is_one_of_the_declared_backends():
    assert normalize_config({})["source"] == "external_comfy"
    assert normalize_config({"source": "cloud"})["source"] == "cloud"
    assert normalize_config({"source": "managed_local"})["source"] == "external_comfy"


def test_seed_fold_round_trips_decimal_and_framework_hex():
    assert fold_seed("18446744073709551615") == 2**64 - 1
    assert fold_seed("ffffffffffffffffffffffffffffffff") == 2**64 - 1
    assert fold_seed("18446744073709551615") == fold_seed(fold_seed("18446744073709551615"))


# ── imported graphs ──────────────────────────────────────────────────────────

_BASE_SLOTS = {"positive": ["0", "text"], "seed": ["s", "seed"], "output": ["o", "images"]}


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
        "slots": slots if slots is not None else {**_BASE_SLOTS, "negative": ["0", "text"]},
    }


def _stored(user_graph: dict) -> dict:
    return normalize_config({"external_comfy": {"user_graphs": [user_graph]}})["external_comfy"]["user_graphs"][0]


def test_user_graphs_are_bounded_by_size_and_count():
    """Oversized or over-count imports are dropped, not stored and half-honoured."""
    oversized = normalize_config({"external_comfy": {"user_graphs": [_user_graph(node_count=6_000)]}})
    assert oversized["external_comfy"]["user_graphs"] == []

    many = normalize_config({"external_comfy": {"user_graphs": [_user_graph(f"user_{i}") for i in range(MAX_USER_GRAPHS + 5)]}})
    assert len(many["external_comfy"]["user_graphs"]) == MAX_USER_GRAPHS


def test_a_user_graph_needs_positive_seed_and_output_but_not_negative_or_checkpoint():
    # A one-encoder prose graph has nothing to map negative to, and a self-contained
    # graph keeps its own model rather than exposing a checkpoint slot.
    stored = _stored(_user_graph(slots=dict(_BASE_SLOTS)))
    assert stored["slots"] == _BASE_SLOTS

    # The model-override slot must survive normalization, or the user's Orb model
    # selection would be silently dropped on save and never reach the graph.
    with_model = _stored(_user_graph(slots={**_BASE_SLOTS, "checkpoint": ["m", "unet_name"]}))
    assert with_model["slots"]["checkpoint"] == ["m", "unet_name"]

    without_seed = _user_graph(slots={"positive": ["0", "text"], "output": ["o", "images"]})
    assert normalize_config({"external_comfy": {"user_graphs": [without_seed]}})["external_comfy"]["user_graphs"] == []


def test_is_changed_is_stripped_from_every_node_at_import():
    """A client-supplied `is_changed` is returned verbatim by IsChangedCache, so a
    hash of the exporter's disk makes ComfyUI miss a file whose *contents* changed
    under an unchanged path and hand back the previously decoded image."""
    graph = _graph()
    graph["0"]["is_changed"] = ["b80d1d64deadbeef"]
    graph["s"]["is_changed"] = ["another"]
    stored = _stored({"id": "user_a", "label": "a", "graph": graph, "slots": dict(_BASE_SLOTS)})
    assert all("is_changed" not in node for node in stored["graph"].values())
    assert stored["graph"]["0"]["inputs"]["text"]  # only the machine-local key goes


# ── reference slots ──────────────────────────────────────────────────────────


def _references(*entries: dict) -> list:
    return _stored(_user_graph(slots={**_BASE_SLOTS, "references": list(entries)}))["slots"].get("references", [])


def test_reference_slots_survive_normalization_and_unstorable_ones_are_dropped():
    stored = _references(
        {"slot": ["72", "image"], "source": "previous_or_character", "label": "Load Image (#72)"},
        {"slot": [90, "image"], "source": "character"},
        {"slot": ["99", "image"], "source": "whatever_the_user_typed"},
    )
    assert stored[0] == {"slot": ["72", "image"], "source": "previous_or_character", "label": "Load Image (#72)"}
    # A numeric node id normalizes to a string, and a missing label gets a usable one.
    assert stored[1]["slot"] == ["90", "image"]
    assert stored[1]["label"]
    # An unknown source is not a slot Orb can fill, so it is not stored as one.
    assert [r["slot"] for r in stored] == [["72", "image"], ["90", "image"]]

    entries = [{"slot": [str(i), "image"], "source": "character"} for i in range(MAX_REFERENCE_SLOTS + 3)]
    assert len(_references(*entries)) == MAX_REFERENCE_SLOTS


def test_a_graph_with_no_references_round_trips_unchanged():
    """A plain text-to-image graph must normalize exactly as it did before
    references existed -- no empty key introduced into its slot map."""
    assert "references" not in _stored(_user_graph(slots=dict(_BASE_SLOTS)))["slots"]


# ── connections ──────────────────────────────────────────────────────────────
#
# A style names the connection it renders on, and `source` is derived from the
# style that will render next. The settings panel deleted its global backend
# picker, so this derivation is the only thing left that decides which adapter
# `get_adapter` builds -- both directions of it are worth pinning.


def _linked(connection: str, *, source: str = "external_comfy", cloud: dict | None = None) -> dict:
    return normalize_config(
        {
            "source": source,
            "default_style": "s",
            "styles": [{"id": "s", "connection": connection}],
            "cloud": cloud or {},
        }
    )


def test_a_style_connection_is_an_id_or_nothing():
    assert _linked("comfy")["styles"][0]["connection"] == "comfy"
    assert _linked("xai")["styles"][0]["connection"] == "xai"
    # Same shape as every other id here; anything else reads as unlinked rather
    # than as a connection nothing will ever resolve.
    assert _linked("../etc/passwd")["styles"][0]["connection"] == ""


def test_the_default_styles_connection_decides_which_backend_routes():
    assert _linked("comfy", source="cloud")["source"] == "external_comfy"

    cloud = _linked("openai", cloud={"providers": {"openai": {"api_key": "k"}}})
    assert cloud["source"] == "cloud"
    # And which provider inside it: the panel has no provider dropdown any more.
    assert cloud["cloud"]["provider"] == "openai"

    # Only the style that renders next decides: a second style pointing elsewhere
    # must not drag the whole config with it.
    two = normalize_config(
        {
            "default_style": "local",
            "styles": [{"id": "local", "connection": "comfy"}, {"id": "remote", "connection": "xai"}],
        }
    )
    assert two["source"] == "external_comfy"


def test_an_unlinked_style_leaves_the_stored_source_alone():
    """The upgrade path. Every existing style carries no connection, and silently
    re-routing one on first read would change what the next image looks like."""
    assert _linked("", source="cloud", cloud={"provider": "xai"})["source"] == "cloud"
    assert _linked("")["source"] == "external_comfy"
    # The shipped defaults are unlinked for exactly this reason.
    assert [s["connection"] for s in normalize_config({})["styles"]] == ["", ""]


# ── the cloud block ──────────────────────────────────────────────────────────


def _cloud(**raw) -> dict:
    return normalize_config({"cloud": raw})["cloud"]


def test_the_provider_map_is_capped_and_keeps_the_selected_entry():
    many = {f"p{i}": {"api_key": f"key-{i}"} for i in range(MAX_CLOUD_PROVIDERS + 5)}
    many["xai"] = {"api_key": "xai-key"}
    cloud = _cloud(provider="xai", providers=many)

    assert len(cloud["providers"]) == MAX_CLOUD_PROVIDERS
    # The cap must never be the reason the credentials in active use go missing.
    assert cloud["providers"]["xai"]["api_key"] == "xai-key"


def test_an_unknown_provider_id_is_retained_with_its_key():
    """Dropping it would be a delete-the-user's-key path, not a tidy-up: the
    normalizer runs on GET, the panel assigns that answer into its shared config,
    and readConfig spreads it back into the next PUT -- so a preset row renamed in a
    later release would erase the stored key on the next save, silently."""
    cloud = _cloud(provider="renamed_in_v2", providers={"renamed_in_v2": {"api_key": "still-mine", "model": "m"}})

    assert cloud["provider"] == "renamed_in_v2"
    # Render settings are per connection, so an entry carrying none of its own is
    # completed from the defaults rather than left short of the shape the adapter reads.
    assert cloud["providers"]["renamed_in_v2"] == {
        "api_key": "still-mine",
        "model": "m",
        "base_url": "",
        "width": 1024,
        "height": 1024,
        "quality": "",
        "reference_source": "",
    }


def test_switching_provider_keeps_the_other_providers_keys():
    stored = _cloud(provider="xai", providers={"xai": {"api_key": "a"}, "openai": {"api_key": "b"}})
    switched = normalize_config({"cloud": {**stored, "provider": "openai"}})["cloud"]
    assert switched["providers"]["xai"]["api_key"] == "a"
    assert switched["providers"]["openai"]["api_key"] == "b"


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://api.example.test/v1", "https://api.example.test/v1"),
        ("https://api.example.test/v1/", "https://api.example.test/v1"),  # trailing slash normalized
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1"),  # a local proxy is the one plaintext case
        ("http://api.example.test/v1", ""),  # a bearer key on every request, in the clear
        ("https://user:secret@api.example.test", ""),
        ("ftp://api.example.test", ""),
        ("not a url", ""),
    ],
)
def test_a_cloud_base_url_override_rejects_credentials_and_plaintext(url, expected):
    stored = _cloud(provider="custom", providers={"custom": {"base_url": url}})
    assert stored["providers"]["custom"]["base_url"] == expected


def test_cloud_render_settings_are_bounded_and_default_to_off():
    assert (_cloud()["width"], _cloud()["height"]) == (1024, 1024)
    bounded = _cloud(width="1536", height=99_999)
    assert (bounded["width"], bounded["height"]) == (1536, 4096)

    assert _cloud(quality="HIGH")["quality"] == "high"
    assert _cloud(quality="ultra")["quality"] == ""
    assert _cloud()["quality"] == ""
    # Sending conversation images to a third party is opt-in.
    assert _cloud()["reference_source"] == ""
    assert _cloud(reference_source="previous")["reference_source"] == "previous"
    assert _cloud(reference_source="whatever")["reference_source"] == ""


def test_render_settings_hoist_into_each_connection_but_its_own_win():
    """The migration for configs written before connections existed: those four
    lived once for "cloud", so every stored connection inherits them on first read
    rather than silently resetting to 1024x1024 with references off."""
    hoisted = _cloud(
        provider="xai",
        width=1536,
        height=1024,
        quality="high",
        reference_source="previous",
        providers={
            # "" is a real answer for both quality and reference_source -- "provider
            # default" and "references off" -- so declaring one must not read as
            # "absent, inherit the old global".
            "openai": {"width": 1024, "height": 1536, "quality": "", "reference_source": ""},
            "xai": {"api_key": "b"},
        },
    )
    openai, xai = hoisted["providers"]["openai"], hoisted["providers"]["xai"]
    assert (openai["width"], openai["height"], openai["quality"], openai["reference_source"]) == (1024, 1536, "", "")
    assert (xai["width"], xai["height"], xai["quality"], xai["reference_source"]) == (1536, 1024, "high", "previous")


def test_the_routing_connections_settings_mirror_to_the_cloud_level():
    """The adapter reads `cloud.width`/`quality`/`reference_source` directly. Keeping
    the mirror is what lets per-connection settings land without the render path, the
    replay record or the attachment metadata changing at all."""
    cloud = normalize_config(
        {
            "default_style": "s",
            "styles": [{"id": "s", "connection": "openai"}],
            "cloud": {
                "provider": "xai",
                "providers": {
                    "xai": {"api_key": "a", "width": 1024, "height": 1024, "quality": "low"},
                    "openai": {"api_key": "b", "width": 1536, "height": 1024, "quality": "high"},
                },
            },
        }
    )["cloud"]
    assert cloud["provider"] == "openai"
    assert (cloud["width"], cloud["height"], cloud["quality"]) == (1536, 1024, "high")


# ── per-character reference image ────────────────────────────────────────────


def test_a_character_reference_image_survives_only_with_both_halves():
    """Bytes Orb cannot tell ComfyUI how to read are not a reference, a mime with
    no bytes is not a half-set field, and an oversized payload is dropped rather
    than truncated -- half a base64 payload is a corrupt image, not a smaller one."""
    kept = normalize_profile({"reference_image_b64": "aGk=", "reference_mime": "image/png"})
    assert (kept["reference_image_b64"], kept["reference_mime"]) == ("aGk=", "image/png")

    for raw in (
        {"reference_image_b64": "aGk=", "reference_mime": "text/plain"},
        {"reference_image_b64": "aGk="},
        {"reference_mime": "image/png"},
        {"reference_image_b64": "A" * (MAX_REFERENCE_IMAGE_B64 + 1), "reference_mime": "image/png"},
    ):
        profile = normalize_profile(raw)
        assert (profile["reference_image_b64"], profile["reference_mime"]) == ("", "")
