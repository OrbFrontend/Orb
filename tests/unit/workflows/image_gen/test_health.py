"""Tests for ComfyUI health verdicts."""

from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine import health

_CONFIG = {"source": "external_comfy", "external_comfy": {"api_url": "http://localhost:8188"}}
_STYLE = {"id": "realistic", "label": "Realistic", "workflow": "user_a", "checkpoint": "sd.safetensors"}


@pytest.fixture(autouse=True)
def _clean():
    health.forget()
    yield
    health.forget()


def test_a_failed_probe_is_remembered_with_the_style_it_was_about():
    health.record_failure(_CONFIG, _STYLE, "The checkpoint 'sd.safetensors' is no longer on the ComfyUI server")

    standing = health.verdict(_CONFIG, _STYLE)
    assert standing is not None
    assert standing["style_id"] == "realistic"
    assert standing["style_label"] == "Realistic"
    assert "sd.safetensors" in standing["detail"]


def test_editing_the_style_retires_the_finding_without_anything_reporting_a_fix():
    health.record_failure(_CONFIG, _STYLE, "checkpoint gone")

    assert health.verdict(_CONFIG, {**_STYLE, "checkpoint": "other.safetensors"}) is None
    # ...and it is dropped, not merely hidden, so undoing the edit cannot resurrect it.
    assert health.verdict(_CONFIG, _STYLE) is None


def test_moving_the_server_retires_the_finding_too():
    health.record_failure(_CONFIG, _STYLE, "checkpoint gone")
    moved = {"source": "external_comfy", "external_comfy": {"api_url": "http://other:8188"}}
    assert health.verdict(moved, _STYLE) is None


def test_a_probe_that_passed_clears_the_finding():
    health.record_failure(_CONFIG, _STYLE, "checkpoint gone")
    health.record_success(_CONFIG, _STYLE)
    assert health.verdict(_CONFIG, _STYLE) is None


def test_the_default_styles_backend_cannot_retire_another_styles_finding():
    pinned = {**_STYLE, "connection": "comfy"}
    config = {**_CONFIG, "styles": [pinned], "default_style": "realistic"}
    health.record_failure(config, pinned, "checkpoint gone")

    elsewhere = {**config, "source": "cloud", "cloud": {"provider": "xai"}}
    assert health.verdict(elsewhere, pinned) is not None


def test_a_cloud_styles_finding_follows_its_own_endpoint_and_key():
    style = {"id": "grok", "label": "Grok", "connection": "xai"}
    config = {"cloud": {"providers": {"xai": {"api_key": "old", "base_url": "https://a.example"}}}}
    health.record_failure(config, style, "unauthorized")
    assert health.verdict(config, style) is not None

    rotated = {"cloud": {"providers": {"xai": {"api_key": "new", "base_url": "https://a.example"}}}}
    assert health.verdict(rotated, style) is None

    health.record_failure(config, style, "unauthorized")
    moved = {"cloud": {"providers": {"xai": {"api_key": "old", "base_url": "https://b.example"}}}}
    assert health.verdict(moved, style) is None


def test_the_api_key_itself_is_never_kept():
    style = {"id": "grok", "label": "Grok", "connection": "xai"}
    config = {"cloud": {"providers": {"xai": {"api_key": "sk-secret-value", "base_url": ""}}}}
    assert "sk-secret-value" not in health.fingerprint(config, style)


def test_a_style_with_no_id_is_not_recorded():
    health.record_failure(_CONFIG, {"label": "Unnamed"}, "boom")
    assert health.verdict(_CONFIG, {"label": "Unnamed"}) is None


def test_the_cache_is_bounded():
    for index in range(health._MAX_ENTRIES + 5):
        health.record_failure(_CONFIG, {**_STYLE, "id": f"s{index}"}, "boom")
    assert len(health._verdicts) <= health._MAX_ENTRIES
