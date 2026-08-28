"""The trust barrier between a stored selection and a child command line.

NOTHING REQUEST-DERIVED REACHES ARGV. A batch size is a key into a closed map
and what comes back is a code-owned literal; a variant is re-resolved against
the registry before its path is allowed anywhere near a subprocess. If either
check were quietly dropped in a refactor, nothing would fail — the barrier
would simply cease to exist — so both are asserted directly.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.features.prose_rewriter import catalog, config
from backend.inference.local_models import assets
from backend.inference.local_models.llama_server import client as C

pytestmark = pytest.mark.asyncio


@pytest.fixture
def downloaded(tmp_path, monkeypatch):
    """Every registered variant, present on disk in a temp models dir."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    for variant in catalog.variants():
        (tmp_path / variant.local_name).write_text("gguf")
    return tmp_path


async def test_a_variant_outside_the_registry_is_refused(downloaded):
    """A request-selected id must resolve to a code-owned registry row before
    any part of it is allowed into the child command."""
    forged = replace(catalog.variants()[0], id="--host", path="/tmp/attacker.gguf")

    with pytest.raises(ValueError, match="Unregistered prose-rewriter variant"):
        config.launch_profile_for(forged, True, 2)


@pytest.mark.parametrize("batch_size", [0, 5, -1])
async def test_a_batch_size_outside_the_supported_range_is_refused(downloaded, batch_size):
    with pytest.raises(ValueError, match="slots must be between 1 and 4"):
        config.launch_profile_for(catalog.variants()[0], True, batch_size)


async def test_a_variant_with_no_file_behind_it_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))
    variant = catalog.variants()[0]

    with pytest.raises(RuntimeError, match="is not downloaded"):
        config.launch_profile_for(variant, True, 4)


async def test_launch_profile_is_a_pure_function_of_the_selection(downloaded):
    """This equality IS the host's load key: two profiles built from the same
    selection compare equal, and any change of model, placement or lane count
    does not."""
    variant, other = catalog.variants()[0], catalog.variants()[1]
    built = config.launch_profile_for(variant, True, 2)

    assert built == config.launch_profile_for(variant, True, 2)
    assert built != config.launch_profile_for(other, True, 2)
    assert built != config.launch_profile_for(variant, False, 2)
    assert built != config.launch_profile_for(variant, True, 3)


@pytest.mark.parametrize("batch_size", [1, 2, 3, 4])
async def test_parallel_slots_select_only_fixed_command_arguments(downloaded, monkeypatch, batch_size):
    """One configured paragraph lane maps to one full CTX_PER_SLOT KV lane, and
    the request-derived number only ever chooses among these literals."""
    monkeypatch.setattr(C.binary_module, "supports_flag", lambda _binary, _flag: False)
    built = config.launch_profile_for(catalog.variants()[0], True, batch_size)
    argv = C._argv(built, C.Path("llama-server"), 12345)

    assert argv[argv.index("--alias") + 1] == "prose-rewriter"
    assert argv[argv.index("--parallel") + 1] == str(batch_size)
    assert argv[argv.index("--ctx-size") + 1] == str(batch_size * config.CTX_PER_SLOT)
    assert argv[argv.index("--threads-http") + 1] == str(batch_size * 2 + 4)


async def test_gpu_placement_is_one_number_on_the_command_line(downloaded):
    variant = catalog.variants()[0]
    assert config.launch_profile_for(variant, True, 1).gpu_layers == 999
    assert config.launch_profile_for(variant, False, 1).gpu_layers == 0


async def test_a_selection_with_nothing_behind_it_is_a_profile_of_none(tmp_path, monkeypatch):
    """The settings paths need "the selection changed" to stay expressible when
    the selection names nothing loadable — that is a stale host, not an error
    to raise at whoever pressed Save."""
    monkeypatch.setattr(assets, "model_dir", lambda: str(tmp_path))

    assert config.profile_for_selection(None, True, 4) is None
    assert config.profile_for_selection(catalog.variants()[0], True, 4) is None


@pytest.mark.parametrize("raw", [1, 2, 3, 4])
async def test_batch_size_selector_maps_supported_input_to_the_closed_allowlist(raw):
    assert config.select_batch_size(raw) == raw


@pytest.mark.parametrize("raw", [0, 5, 2.5, "2", True, None])
async def test_batch_size_selector_rejects_everything_outside_the_closed_allowlist(raw):
    assert config.select_batch_size(raw) is None


# ── the persisted blob → the turn's config ───────────────────────────────────


async def test_turn_config_resolves_the_persisted_batch_size(monkeypatch):
    monkeypatch.setattr(config, "runnable", lambda _variant: True)

    resolved = config.resolve_config(
        {
            "local_ml_config": {
                "prose_rewriter": {"variant": "1.7b-q8", "gpu": False, "batch_size": 2},
            }
        }
    )

    assert resolved == {"variant_id": "1.7b-q8", "gpu": False, "batch_size": 2}


async def test_turn_config_defaults_an_old_or_malformed_batch_size(monkeypatch):
    monkeypatch.setattr(config, "runnable", lambda _variant: True)
    base = {"variant": "1.7b-q8", "gpu": True}

    old = config.resolve_config({"local_ml_config": {"prose_rewriter": base}})
    malformed = config.resolve_config({"local_ml_config": {"prose_rewriter": {**base, "batch_size": 99}}})

    assert old is not None and old["batch_size"] == 4
    assert malformed is not None and malformed["batch_size"] == 4
