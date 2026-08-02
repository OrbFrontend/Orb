"""Source routing. The interesting cases are the ones that must not raise."""

from __future__ import annotations

from backend.workflows.image_gen.config import normalize_config
from backend.workflows.image_gen.engine import comfy_adapter, get_adapter, list_sources
from backend.workflows.image_gen.engine.adapters.external_comfy import (
    ExternalComfyAdapter,
)
from backend.workflows.image_gen.engine.adapters.openai_image import (
    OpenAICompatibleImageAdapter,
)


def test_each_source_routes_to_its_own_adapter():
    assert isinstance(get_adapter(normalize_config({})), ExternalComfyAdapter)
    assert isinstance(get_adapter(normalize_config({"source": "cloud"})), OpenAICompatibleImageAdapter)


def test_an_unknown_source_falls_back_rather_than_raising():
    """`source` reaches this from a stored config. A hand-edited DB should degrade
    to the default backend, not turn every page load into a 500."""
    assert isinstance(get_adapter({"source": "managed_local"}), ExternalComfyAdapter)
    assert isinstance(get_adapter({}), ExternalComfyAdapter)


def test_the_comfy_adapter_is_reachable_while_another_source_is_active():
    """Imported graphs are global and the importer stays usable under cloud, so
    `node_types` dispatches by name -- never by the active source."""
    config = normalize_config({"source": "cloud"})
    adapter = comfy_adapter(config)
    assert isinstance(adapter, ExternalComfyAdapter)
    assert hasattr(adapter, "node_roles")


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
    cloud = get_adapter(
        normalize_config({"source": "cloud", "cloud": {"provider": "xai", "providers": {"xai": {"api_key": "k"}}}})
    )
    assert cloud.display_name == "Cloud API"
    assert cloud.label == "xAI (Grok)"
    # Unchanged for ComfyUI, so stored `source` values do not shift.
    assert get_adapter(normalize_config({})).label == "External ComfyUI"
