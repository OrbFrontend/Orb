from __future__ import annotations

import json

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
    "UNETLoader": {"input": {"required": {"unet_name": [["real.safetensors"]], "weight_dtype": [["default"], {}]}}},
    "EmptyLatentImage": {"input": {"required": {}}},
    "CLIPTextEncode": {"input": {"required": {}}},
    "VAEDecode": {"input": {"required": {}}},
    "SaveImage": {"input": {"required": {}}, "output_node": True},
}

# An imported graph that loads its diffusion model via UNETLoader and pins a
# filename from the machine that exported the PNG ("gone.safetensors"). The
# `checkpoint` slot marks the input Orb's model selection overrides.
UNET_USER_GRAPH = {
    "id": "user_unet",
    "label": "Unet",
    "graph": {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "gone.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "4": {"class_type": "KSampler", "inputs": {"seed": 0}},
        "5": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    },
    "slots": {
        "positive": ["2", "text"],
        "negative": ["3", "text"],
        "seed": ["4", "seed"],
        "output": ["5", "images"],
        "checkpoint": ["1", "unet_name"],
    },
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
    config = normalize_config({"external_comfy": {"styles": [{"id": "s", "label": "S", "checkpoint": "anime.safetensors"}]}})

    result = await external_comfy.validate_connection(config)

    assert result["ok"] is True
    assert result["models"] == ["anime.safetensors", "z.safetensors"]


@pytest.mark.asyncio
async def test_connection_stays_valid_when_model_discovery_fails(monkeypatch):
    # A user graph carries its own loaders, so nothing about this connection
    # depends on the model list the settings dropdown would like to show.
    _install_client(monkeypatch, _handler(httpx.Response(403)))
    config = normalize_config(
        {"external_comfy": {"user_graphs": [USER_GRAPH], "styles": [{"id": "s", "label": "S", "workflow": "user_1"}]}}
    )

    result = await external_comfy.validate_connection(config)

    assert result["ok"] is True
    assert result["models"] == []


@pytest.mark.asyncio
async def test_user_graph_model_override_is_validated_not_the_imported_pin(monkeypatch):
    # The graph pins "gone.safetensors" from another machine; the user's checkpoint
    # overrides it, so validation must check the model that will actually run.
    _install_client(monkeypatch, _handler(httpx.Response(200, json=["real.safetensors"])))
    config = normalize_config(
        {
            "external_comfy": {
                "user_graphs": [UNET_USER_GRAPH],
                "styles": [{"id": "s", "label": "S", "workflow": "user_unet", "checkpoint": "real.safetensors"}],
            }
        }
    )

    result = await external_comfy.validate_connection(config)

    assert result["ok"] is True


@pytest.mark.asyncio
async def test_user_graph_without_override_surfaces_the_missing_model(monkeypatch):
    # No checkpoint slot: the graph keeps its own (missing) model, and Test
    # connection reports it rather than letting it fail cryptically at render.
    from backend.workflows.image_gen.engine.contracts import ImageGenerationError

    _install_client(monkeypatch, _handler(httpx.Response(200, json=["real.safetensors"])))
    graph = {**UNET_USER_GRAPH, "slots": {k: v for k, v in UNET_USER_GRAPH["slots"].items() if k != "checkpoint"}}
    config = normalize_config(
        {"external_comfy": {"user_graphs": [graph], "styles": [{"id": "s", "label": "S", "workflow": "user_unet"}]}}
    )

    with pytest.raises(ImageGenerationError, match="no longer available"):
        await external_comfy.validate_connection(config)


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
async def test_node_roles_type_upload_widgets_off_the_image_upload_flag(monkeypatch):
    """An upload widget's declared type is the *combo* of files already in the
    server's input directory, so no string-kind comparison can find it; the
    `image_upload` flag is the typing rule."""
    info = {
        "LoadImage": {
            "input": {
                "required": {"image": [["a.png", "b.png"], {"image_upload": True}]},
                "optional": {"channel": [["red", "green"], {}]},
            }
        },
        "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["m.safetensors"], {}]}}},
    }
    _install_client(monkeypatch, lambda _request: httpx.Response(200, json=info))
    invalidate_object_info()
    roles = await external_comfy.node_roles(normalize_config({}), ["LoadImage", "CheckpointLoaderSimple"])

    assert roles["LoadImage"]["image_inputs"] == ["image"]
    # A plain combo (`channel`, `ckpt_name`) is a fixed menu, not an upload slot.
    assert roles["CheckpointLoaderSimple"]["image_inputs"] == []


# USER_GRAPH plus a LoadImage pinning a filename from the exporting machine.
EDIT_USER_GRAPH = {
    **USER_GRAPH,
    "id": "user_edit",
    "graph": {**USER_GRAPH["graph"], "0": {"class_type": "LoadImage", "inputs": {"image": "exported-elsewhere.png"}}},
    "slots": {
        **USER_GRAPH["slots"],
        "references": [{"slot": ["0", "image"], "source": "character", "label": "Load Image (#0)"}],
    },
}


@pytest.mark.asyncio
async def test_generate_uploads_each_reference_once_and_patches_the_widget(monkeypatch):
    from backend.workflows.image_gen.engine.contracts import (
        ImageRequest,
        ResolvedReference,
    )

    uploads: list[str] = []
    submitted: dict = {}
    outputs = {"4": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}}
    responses = {
        "/prompt": httpx.Response(200, json={"prompt_id": "p1", "number": 1}),
        "/queue": httpx.Response(200, json={"queue_running": [], "queue_pending": []}),
        "/history/p1": httpx.Response(200, json={"p1": {"status": {"completed": True}, "outputs": outputs}}),
        "/view": httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"out", headers={"content-type": "image/png"}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            uploads.append(request.content.decode("latin-1"))
            return httpx.Response(200, json={"name": "orb_deadbeefdeadbeef.png", "subfolder": "orb", "type": "input"})
        if request.url.path == "/prompt":
            submitted.update(json.loads(request.content))
        return responses.get(request.url.path, httpx.Response(404))

    _install_client(monkeypatch, handler)
    config = normalize_config(
        {"external_comfy": {"user_graphs": [EDIT_USER_GRAPH], "styles": [{"id": "s", "label": "S", "workflow": "user_edit"}]}}
    )
    reference = ResolvedReference(
        slot=("0", "image"),
        source="character",
        data=b"\x89PNG\r\n\x1a\n" + b"ref",
        mime="image/png",
        origin="character:card-1",
        digest="deadbeefdeadbeef" + "0" * 48,
    )
    request = ImageRequest(
        prompt="p",
        negative_prompt="",
        seed=1,
        style_id="s",
        timeout_seconds=5,
        # Two slots, one image: the adapter must upload once.
        references=(reference, reference),
    )

    result = await external_comfy.generate(config, request, checkpoint="", graph_id="user_edit")

    assert len(uploads) == 1
    assert submitted["prompt"]["0"]["inputs"]["image"] == "orb/orb_deadbeefdeadbeef.png"
    # The replay record names where the bytes came from, not just what was sent.
    assert result.backend_info["references"][0]["origin"] == "character:card-1"
    assert result.backend_info["references"][0]["comfy_name"] == "orb/orb_deadbeefdeadbeef.png"


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
    config = normalize_config({"external_comfy": {"styles": [{"id": "s", "label": "S", "checkpoint": "anime.safetensors"}]}})

    await external_comfy.validate_connection(config, allow_cached=True)
    await external_comfy.validate_connection(config, allow_cached=True)
    assert calls["object_info"] == 1, "the readiness probe must not refetch the node catalogue every modal open"

    # Pressing Test connection means "look again".
    await external_comfy.validate_connection(config, allow_cached=False)
    assert calls["object_info"] == 2
