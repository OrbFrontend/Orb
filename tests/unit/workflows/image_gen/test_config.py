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
    # graph keeps its own model rather than exposing a checkpoint slot. Exact
    # equality: normalization introduces no empty `references` key either.
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


def test_a_graph_stores_which_inputs_load_an_image_and_never_where_from():
    """The split this whole feature turns on. Which node inputs load an image is
    structural -- discovered at import against `/object_info`, unchangeable without
    re-importing -- so the graph keeps it. Where each one draws from is a *style's*
    answer, so no `source` survives here; two styles on one workflow can differ and
    either can switch a slot off."""
    stored = _references(
        {"slot": ["72", "image"], "source": "previous_or_character", "label": "Load Image (#72)"},
        {"slot": [90, "image"]},
        {"slot": ["99"]},
    )
    assert stored[0] == {"slot": ["72", "image"], "label": "Load Image (#72)"}
    # A numeric node id normalizes to a string, and a missing label gets a usable one.
    assert stored[1]["slot"] == ["90", "image"]
    assert stored[1]["label"]
    # A malformed slot names no widget to patch, so it is not stored as one.
    assert [r["slot"] for r in stored] == [["72", "image"], ["90", "image"]]

    entries = [{"slot": [str(i), "image"]} for i in range(MAX_REFERENCE_SLOTS + 3)]
    assert len(_references(*entries)) == MAX_REFERENCE_SLOTS


def test_one_entry_per_widget_so_the_style_answers_a_stable_position():
    """Two rows on one slot would both resolve and both be recorded, but only the
    second survives patching -- and, now that the style answers positionally, the
    duplicate would silently consume the answer meant for the next slot."""
    stored = _references(
        {"slot": ["72", "image"], "label": "first"},
        {"slot": ["72", "image"], "label": "again"},
        {"slot": ["73", "image"], "label": "second"},
    )
    assert [r["label"] for r in stored] == ["first", "second"]


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
    # A connection is an address and a credential, exactly as wide as the ComfyUI
    # one's {api_url, api_key}. `model` was here and belongs to the style now, so it
    # is read on the way past and not written back -- which is the migration.
    assert cloud["providers"]["renamed_in_v2"] == {"api_key": "still-mine", "base_url": ""}


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


# ── the render target, on the style ──────────────────────────────────────────
#
# A connection is how Orb reaches a backend; a style is what an image looks like.
# The four cloud render settings lived on the connection, which made a connection a
# render preset that happened to hold a key -- and made "FLUX.1-kontext for realistic,
# SDXL for anime, both on Together AI" unreachable, since the credential map is keyed
# by provider id and allows one connection per provider.


def _style(**raw) -> dict:
    return normalize_config({"default_style": "s", "styles": [{"id": "s", **raw}]})["styles"][0]


def test_a_styles_render_settings_are_bounded_and_default_to_off():
    assert (_style()["width"], _style()["height"]) == (1024, 1024)
    bounded = _style(width="1536", height=99_999)
    assert (bounded["width"], bounded["height"]) == (1536, 4096)

    assert _style(quality="HIGH")["quality"] == "high"
    assert _style(quality="ultra")["quality"] == ""
    assert _style()["quality"] == ""
    # Compatibility controls are opt-outs so an existing configuration keeps its
    # request shape until the user changes it.
    assert _style()["send_seed"] is True
    assert _style()["seed_max"] == ""
    assert _style()["send_negative_prompt"] is True
    manual = _style(send_seed=False, seed_max="2147483647", send_negative_prompt=False)
    assert manual["send_seed"] is False
    assert manual["seed_max"] == str(2**31 - 1)
    assert manual["send_negative_prompt"] is False
    assert _style(seed_max="not a number")["seed_max"] == ""
    assert _style(seed_max=True)["seed_max"] == ""
    assert _style(seed_max=2**64 - 1)["seed_max"] == "18446744073709551615"
    # "" resolves to the provider's own default at the adapter, so relinking to a
    # provider with a different one needs no rewrite here.
    assert _style()["model"] == ""
    assert _style(model="black-forest-labs/FLUX.1-kontext-pro")["model"] == "black-forest-labs/FLUX.1-kontext-pro"
    # Sending conversation images anywhere is opt-in, and it is one source policy per
    # style. A multi-input target can apply that policy to several round speakers.
    assert _style()["reference_source"] == ""
    assert _style(reference_source="previous")["reference_source"] == "previous"
    assert _style(reference_source="whatever")["reference_source"] == ""
    assert _style(reference_source=["previous"])["reference_source"] == ""
    # The retired cast sources asked for a likeness of somebody in the scene; the one
    # that survives is the character's own, so a stored row keeps sending a picture
    # rather than silently reading as prompt-only.
    assert _style(reference_source="cast")["reference_source"] == "character"
    assert _style(reference_source="cast_or_character")["reference_source"] == "character"
    # The combining choice targets can honour positionally: every character, then the
    # chat image, bounded by the available reference inputs.
    assert _style(reference_source="character_and_previous")["reference_source"] == "character_and_previous"


def test_both_backend_halves_are_kept_whichever_connection_is_linked():
    """Relinking cloud -> ComfyUI -> cloud must lose neither pin, the rule
    `checkpoint`/`workflow` already followed. Only which half is *rendered* swaps."""
    style = _style(connection="comfy", checkpoint="anime.safetensors", model="gpt-image-1", quality="high")
    assert (style["checkpoint"], style["model"], style["quality"]) == ("anime.safetensors", "gpt-image-1", "high")


def test_render_settings_hoist_from_the_connection_onto_each_style_that_links_it():
    """The migration. Those four lived on the connection, so every style linked to one
    inherits them on first read rather than silently resetting to 1024x1024 on the
    provider default -- and persists hoisted on the first write, since the connection
    entry stops carrying them."""
    config = normalize_config(
        {
            "default_style": "realistic",
            "styles": [
                {"id": "realistic", "connection": "togetherai"},
                {"id": "anime", "connection": "togetherai"},
                {"id": "square", "connection": "togetherai", "width": 1024, "height": 1024, "quality": ""},
                {"id": "local", "connection": "comfy"},
            ],
            "cloud": {
                "provider": "xai",
                "width": 1536,
                "height": 1024,
                "providers": {
                    "togetherai": {
                        "api_key": "k",
                        "model": "black-forest-labs/FLUX.1-schnell",
                        "width": 1024,
                        "height": 1536,
                        "quality": "high",
                        "reference_source": "previous",
                    }
                },
            },
        }
    )
    styles = {s["id"]: s for s in config["styles"]}
    # Both styles on that one connection take its model, which is exactly the state
    # the old shape could not express -- and which they can now diverge from.
    for sid in ("realistic", "anime"):
        assert styles[sid]["model"] == "black-forest-labs/FLUX.1-schnell"
        assert (styles[sid]["width"], styles[sid]["height"]) == (1024, 1536)
        assert (styles[sid]["quality"], styles[sid]["reference_source"]) == ("high", "previous")
    # Membership, not truthiness: "" is a real answer for quality ("provider
    # default"), so a style declaring one must not read as "absent, inherit".
    assert (styles["square"]["width"], styles["square"]["height"], styles["square"]["quality"]) == (1024, 1024, "")
    # A ComfyUI-linked style names no cloud entry, so it falls through to the legacy
    # top level. Inert until its graph maps size slots, which is opt-in at import.
    assert (styles["local"]["width"], styles["local"]["height"]) == (1536, 1024)
    # And the entry keeps only what a connection is.
    assert config["cloud"]["providers"]["togetherai"] == {"api_key": "k", "base_url": ""}


def _migrated_config(*, style: dict, references: list[dict], cloud: dict | None = None) -> dict:
    graph = _user_graph("user_a", slots={**_BASE_SLOTS, "references": references})
    return normalize_config(
        {
            "default_style": "s",
            "styles": [{"id": "s", **style}],
            "external_comfy": {"user_graphs": [graph]},
            "cloud": cloud or {},
        }
    )


def _migrated(**kwargs) -> dict:
    return _migrated_config(**kwargs)["styles"][0]


def test_a_graphs_own_per_slot_sources_hoist_onto_every_style_that_names_it():
    """The other half of the migration. Those sources lived on the graph, where they
    were fixed at import and shared by every style using it; they land on the style in
    the same order the graph declares its slots, so an upgraded install renders exactly
    as it did and can then diverge per style."""
    style = _migrated(
        style={"connection": "comfy", "workflow": "user_a"},
        references=[
            {"slot": ["11", "image"], "source": "previous", "label": "Load Image (#11)"},
            {"slot": ["31", "image"], "source": "character", "label": "IPAdapter (#31)"},
        ],
    )
    # One source policy now, so the slot every target has -- the first -- is the one
    # that survives. At render time it is applied positionally across the round cast.
    assert style["reference_source"] == "previous"


def test_a_comfyui_style_does_not_inherit_the_cloud_blocks_reference_setting():
    """`reference_source` reached *every* style through the raw cloud block, whatever
    it was linked to, because nothing but the cloud adapter ever read it. Honouring it
    for a graph-bound style would silently start uploading conversation images to a
    ComfyUI server on the strength of a setting made for a commercial API."""
    cloud = {"provider": "xai", "reference_source": "character", "providers": {"xai": {"api_key": "k"}}}
    on_comfy = _migrated(style={"connection": "comfy", "workflow": "user_a"}, references=[], cloud=cloud)
    assert on_comfy["reference_source"] == ""
    # The style it *was* made for still inherits it.
    on_cloud = _migrated(style={"connection": "xai"}, references=[], cloud=cloud)
    assert on_cloud["reference_source"] == "character"


def test_the_style_wins_over_both_legacy_shapes_and_the_migration_is_idempotent():
    style = {"connection": "comfy", "workflow": "user_a", "reference_source": "character"}
    references = [{"slot": ["11", "image"], "source": "previous", "label": "Load Image (#11)"}]
    assert _migrated(style=style, references=references)["reference_source"] == "character"

    # The list shape it replaced still out-ranks the graph's, so an install that
    # upgraded once and has not been opened since does not fall back a step further.
    listed = _migrated(
        style={"connection": "comfy", "workflow": "user_a", "reference_sources": ["character"]}, references=references
    )
    assert listed["reference_source"] == "character"

    # Membership, not truthiness: a style that has switched its reference off must not
    # read as "absent, inherit" on the next read and turn it back on.
    off = _migrated(style={**style, "reference_source": ""}, references=references)
    assert off["reference_source"] == ""

    # A fixed point: the hoist happens on the first read and the first write persists
    # it, so re-normalizing what came out must change nothing. Without this the graph's
    # legacy source would keep out-ranking a style that had since switched the slot off.
    hoisted = _migrated_config(style={"connection": "comfy", "workflow": "user_a"}, references=references)
    assert normalize_config(hoisted) == hoisted


def test_the_legacy_sources_come_from_the_graph_row_that_survived_parsing():
    """`_unique_by_id` keeps the first candidate that *parses*, so the sources have to
    be recorded on that same pass. Collected by a second walk of the raw list, a
    discarded row claiming the id would answer for the graph the style renders on."""
    references = [{"slot": ["11", "image"], "source": "previous", "label": "Load Image (#11)"}]
    kept = _user_graph("user_a", slots={**_BASE_SLOTS, "references": references})
    config = normalize_config(
        {
            "default_style": "s",
            "styles": [{"id": "s", "connection": "comfy", "workflow": "user_a"}],
            # Same id, unparseable, and first -- so it is the one a raw walk would find.
            "external_comfy": {"user_graphs": [{**kept, "graph": "not a graph", "slots": dict(_BASE_SLOTS)}, kept]},
        }
    )
    assert config["styles"][0]["reference_source"] == "previous"


def test_an_unlinked_style_inherits_from_the_connection_it_actually_renders_on():
    """A style predating connection linking renders on `cloud.provider`, so that is
    the entry it must inherit from -- which is what makes the migration a no-op for
    what the next Visualize produces."""
    config = normalize_config(
        {
            "source": "cloud",
            "default_style": "s",
            "styles": [{"id": "s", "connection": ""}],
            "cloud": {
                "provider": "openai",
                "providers": {
                    "openai": {"api_key": "a", "model": "gpt-image-1", "width": 1536, "height": 1024},
                    "xai": {"api_key": "b", "model": "grok-imagine-image"},
                },
            },
        }
    )
    style = config["styles"][0]
    assert style["model"] == "gpt-image-1"
    assert (style["width"], style["height"]) == (1536, 1024)


def test_a_config_that_never_stored_a_style_still_inherits_its_cloud_settings():
    """The shipped defaults are parsed like any other style rather than copied in
    whole. Declaring a size on them would out-rank the legacy block they must inherit
    from, and an install that configured cloud before styles were ever written would
    silently reset to 1024x1024 on the read that migrates it."""
    config = normalize_config(
        {
            "source": "cloud",
            "cloud": {"provider": "xai", "width": 1536, "height": 1024, "providers": {"xai": {"api_key": "k"}}},
        }
    )
    assert [s["id"] for s in config["styles"]] == ["realistic", "anime"]
    assert all((s["width"], s["height"]) == (1536, 1024) for s in config["styles"])


def test_a_connection_no_style_links_loses_its_model_and_keeps_its_key():
    """Nothing renders there, so there is nothing to hoist the model onto. Linking a
    style later seeds from the preset's own default, resolved at the adapter."""
    cloud = _cloud(provider="xai", providers={"openai": {"api_key": "kept", "model": "gpt-image-1"}})
    assert cloud["providers"]["openai"] == {"api_key": "kept", "base_url": ""}


def test_the_cloud_block_is_connectivity_only():
    """The four render settings were mirrored to `cloud.*` because that is where the
    adapter read them. It reads the style now, so the mirror is gone -- and with it
    the shape that made two models on one provider a second connection."""
    cloud = normalize_config(
        {
            "default_style": "s",
            "styles": [{"id": "s", "connection": "openai", "width": 1536, "height": 1024}],
            "cloud": {"provider": "xai", "providers": {"openai": {"api_key": "b"}}},
        }
    )["cloud"]
    # `provider` survives as the legacy fallback for an unlinked style, and still
    # records which connection routes.
    assert set(cloud) == {"provider", "providers"}
    assert cloud["provider"] == "openai"


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
