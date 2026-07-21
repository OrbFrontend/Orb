from __future__ import annotations

import httpx
import pytest

from backend.workflows.image_gen.config import normalize_config
from backend.workflows.image_gen.engine.adapters import external_comfy
from backend.workflows.image_gen.engine.comfy_client import (
    ComfyClient,
    invalidate_object_info,
)

OBJECT_INFO = {
    "KSampler": {"input": {"required": {}}},
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["anime.safetensors"]]}}},
    "EmptyLatentImage": {"input": {"required": {}}},
    "CLIPTextEncode": {"input": {"required": {}}},
    "VAEDecode": {"input": {"required": {}}},
    "SaveImage": {"input": {"required": {}}, "output_node": True},
}

USER_GRAPH = {
    "id": "user_1",
    "label": "Mine",
    "graph": {
        "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "3": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0]}},
    },
    "slots": {
        "positive": ["1", "text"],
        "negative": ["2", "text"],
        "seed": ["3", "seed"],
        "output": ["4", "images"],
    },
}


def _install_client(monkeypatch, handler) -> None:
    def factory(config):
        return ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))

    monkeypatch.setattr(external_comfy, "_client", factory)


def _handler(models_response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(
                200,
                json={
                    "system": {"comfyui_version": "0.22.0"},
                    "devices": [{"name": "RTX 3090", "vram_total": 1}],
                },
            )
        if request.url.path == "/object_info":
            return httpx.Response(200, json=OBJECT_INFO)
        if request.url.path == "/models/checkpoints":
            return models_response
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_connection_test_returns_discovered_checkpoints(monkeypatch):
    _install_client(
        monkeypatch,
        _handler(httpx.Response(200, json=["z.safetensors", "anime.safetensors"])),
    )
    config = normalize_config(
        {
            "external_comfy": {
                "checkpoint": "anime.safetensors",
                "workflow": "external_core",
            }
        }
    )

    result = await external_comfy.validate_connection(config)

    assert result["ok"] is True
    assert result["models"] == ["anime.safetensors", "z.safetensors"]


@pytest.mark.asyncio
async def test_connection_stays_valid_when_model_discovery_fails(monkeypatch):
    # A user graph carries its own loaders, so nothing about this connection
    # depends on the model list the settings dropdown would like to show.
    _install_client(monkeypatch, _handler(httpx.Response(403)))
    config = normalize_config({"external_comfy": {"workflow": "user_1", "user_graphs": [USER_GRAPH]}})

    result = await external_comfy.validate_connection(config)

    assert result["ok"] is True
    assert result["models"] == []


@pytest.mark.asyncio
async def test_node_roles_type_slots_from_object_info_and_skip_unknown_classes(monkeypatch):
    """The picker's typing crosses the wire as a verdict, not as /object_info.

    A real install reports ~2000 node types, tens of megabytes; handing that to a
    browser to populate four dropdowns is what this route exists to avoid.
    """
    info = {
        "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}], "clip": ["CLIP"]}}},
        "KSampler": {"input": {"required": {"seed": ["INT", {}], "steps": ["INT", {}]}}},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/object_info"
        return httpx.Response(200, json=info)

    _install_client(monkeypatch, handler)
    invalidate_object_info()
    roles = await external_comfy.node_roles(normalize_config({}), ["CLIPTextEncode", "KSampler", "SaveImage", "Nope"])

    assert roles["CLIPTextEncode"]["text_inputs"] == ["text"]
    # `steps` is an INT too; only seed-named ones are seed candidates.
    assert roles["KSampler"]["seed_inputs"] == ["seed"]
    assert roles["SaveImage"]["output_node"] is True
    assert "Nope" not in roles


@pytest.mark.asyncio
async def test_object_info_is_cached_for_probes_and_refetched_on_an_explicit_test(monkeypatch):
    calls = {"object_info": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/object_info":
            calls["object_info"] += 1
            return httpx.Response(200, json=OBJECT_INFO)
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "devices": []})
        if request.url.path == "/models/checkpoints":
            return httpx.Response(200, json=["anime.safetensors"])
        return httpx.Response(404)

    _install_client(monkeypatch, handler)
    invalidate_object_info()
    config = normalize_config({"external_comfy": {"checkpoint": "anime.safetensors"}})

    await external_comfy.validate_connection(config, allow_cached=True)
    await external_comfy.validate_connection(config, allow_cached=True)
    assert calls["object_info"] == 1, "the readiness probe must not refetch the node catalogue every modal open"

    # Pressing Test connection means "look again".
    await external_comfy.validate_connection(config, allow_cached=False)
    assert calls["object_info"] == 2
