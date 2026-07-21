from __future__ import annotations

import httpx
import pytest

from backend.workflows.image_gen.config import normalize_config
from backend.workflows.image_gen.engine.adapters import external_comfy
from backend.workflows.image_gen.engine.comfy_client import ComfyClient

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
