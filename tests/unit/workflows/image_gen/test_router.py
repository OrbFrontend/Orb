"""Source routing. The interesting cases are the ones that must not raise."""

from __future__ import annotations

from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.engine import comfy_adapter, get_adapter, list_sources
from backend.workflows.image_gen.engine.adapters.external_comfy import (
    ExternalComfyAdapter,
)
from backend.workflows.image_gen.engine.adapters.openai_image import (
    OpenAICompatibleImageAdapter,
)


def _routed(config: dict, style_id: str):
    return get_adapter(config, resolve_style(config, style_id))


def test_each_connection_routes_to_its_own_adapter():
    comfy = normalize_config({"default_style": "s", "styles": [{"id": "s", "connection": "comfy"}]})
    cloud = normalize_config({"default_style": "s", "styles": [{"id": "s", "connection": "xai"}]})
    assert isinstance(_routed(comfy, "s"), ExternalComfyAdapter)
    assert isinstance(_routed(cloud, "s"), OpenAICompatibleImageAdapter)


def test_an_unlinked_style_still_follows_the_stored_global_source():
    """The upgrade path: every style predating connection linking carries `""`, and
    silently re-routing one would change what the next image looks like."""
    assert isinstance(_routed(normalize_config({}), "realistic"), ExternalComfyAdapter)
    cloud = normalize_config({"source": "cloud", "cloud": {"provider": "xai", "providers": {"xai": {"api_key": "k"}}}})
    assert isinstance(_routed(cloud, "realistic"), OpenAICompatibleImageAdapter)


def test_routing_follows_the_style_being_rendered_not_the_default_one():
    """The rehydrate bug this fixes. `normalize_config` derives `source` from the
    *default* style, and `/rehydrate` calls the hook with the attachment's stored
    `style_id` -- so a ComfyUI-linked style replayed while the default style is
    cloud-linked went to the cloud adapter. It survived only because that adapter
    ignored the style it was handed, which is no longer true.
    """
    config = normalize_config(
        {
            "default_style": "remote",
            "styles": [
                {"id": "remote", "connection": "xai"},
                {"id": "local", "connection": "comfy", "checkpoint": "anime.safetensors"},
            ],
            "cloud": {"providers": {"xai": {"api_key": "k"}}},
        }
    )
    assert config["source"] == "cloud"
    assert isinstance(_routed(config, "remote"), OpenAICompatibleImageAdapter)
    assert isinstance(_routed(config, "local"), ExternalComfyAdapter)


def test_two_styles_on_one_provider_can_name_two_models():
    """The case the old shape could not express at all: `cloud.providers` is keyed by
    provider id, so a second model meant a second connection, and the panel allows
    one per provider."""
    config = normalize_config(
        {
            "default_style": "realistic",
            "styles": [
                {"id": "realistic", "connection": "togetherai", "model": "black-forest-labs/FLUX.1-kontext-pro"},
                {"id": "anime", "connection": "togetherai", "model": "black-forest-labs/FLUX.1-schnell"},
            ],
            "cloud": {"providers": {"togetherai": {"api_key": "k"}}},
        }
    )
    targets = [_routed(config, sid).resolve_target(None) for sid in ("realistic", "anime")]
    assert [t.model for t in targets] == ["black-forest-labs/FLUX.1-kontext-pro", "black-forest-labs/FLUX.1-schnell"]


def test_an_unknown_source_falls_back_rather_than_raising():
    """`source` reaches this from a stored config. A hand-edited DB should degrade
    to the default backend, not turn every page load into a 500."""
    assert isinstance(get_adapter({"source": "managed_local"}, {}), ExternalComfyAdapter)
    assert isinstance(get_adapter({}, {}), ExternalComfyAdapter)


def test_the_comfy_adapter_is_reachable_while_a_style_renders_elsewhere():
    """Imported graphs are global and the importer stays usable under cloud, so
    `node_types` dispatches by name -- never by a style's connection. It also passes
    no style: `node_roles` is a pure network question with no render target."""
    config = normalize_config({"source": "cloud"})
    adapter = comfy_adapter(config)
    assert isinstance(adapter, ExternalComfyAdapter)
    assert hasattr(adapter, "node_roles")
    # Constructed without one, it still has a style to answer readiness about.
    assert adapter.style["id"] == config["default_style"]


def test_sources_are_listed_without_building_a_client():
    """`status` answers a menu; it has no business constructing adapters, which is
    why this reads ClassVars rather than instantiating the way TTS does."""
    sources = list_sources()
    assert {source["id"] for source in sources} == {"external_comfy", "cloud"}
    for source in sources:
        assert source["label"]
        assert set(source["capabilities"]) >= {"can_generate", "supports_seed", "supports_references"}


def test_the_configured_adapter_labels_itself_by_provider_not_by_backend():
    """`display_name` names the backend for the picker; `label` names the thing
    actually rendering, which is what lands on an attachment."""
    config = normalize_config(
        {"default_style": "s", "styles": [{"id": "s", "connection": "xai"}], "cloud": {"providers": {"xai": {"api_key": "k"}}}}
    )
    cloud = _routed(config, "s")
    assert cloud.display_name == "Cloud API"
    assert cloud.label == "xAI (Grok)"
    # Unchanged for ComfyUI, so stored `source` values do not shift.
    assert _routed(normalize_config({}), "realistic").label == "External ComfyUI"
