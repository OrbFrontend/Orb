from __future__ import annotations

from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.hooks import fold_seed


def test_config_normalizes_external_source_and_seeded_styles():
    cfg = normalize_config({})
    assert cfg["source"] == "external_comfy"
    assert [s["id"] for s in cfg["external_comfy"]["styles"]] == ["realistic", "anime"]
    assert resolve_style(cfg, "anime")["prompt"].startswith("anime illustration")


def test_config_rejects_credentials_in_url_and_bounds_timeout():
    cfg = normalize_config(
        {
            "timeout_seconds": "9999",
            "external_comfy": {"api_url": "http://user:secret@example.test:8188"},
        }
    )
    assert cfg["external_comfy"]["api_url"] == "http://127.0.0.1:8188"
    assert cfg["timeout_seconds"] == 900.0


def test_seed_fold_round_trips_decimal_and_framework_hex():
    assert fold_seed("18446744073709551615") == 2**64 - 1
    assert fold_seed("ffffffffffffffffffffffffffffffff") == 2**64 - 1
    assert fold_seed("18446744073709551615") == fold_seed(fold_seed("18446744073709551615"))
