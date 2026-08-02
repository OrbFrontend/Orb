from __future__ import annotations

import json

import httpx
import pytest

from backend.workflows.image_gen.config import normalize_config, resolve_style
from backend.workflows.image_gen.engine.adapters.external_comfy import (
    ExternalComfyAdapter,
)
from backend.workflows.image_gen.engine.comfy_client import (
    ComfyClient,
    invalidate_object_info,
)
from backend.workflows.image_gen.engine.contracts import (
    ImageGenerationError,
    ImageRequest,
    ResolvedReference,
)

OBJECT_INFO = {
    "KSampler": {"input": {"required": {"seed": ["INT", {}], "steps": ["INT", {}]}}},
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["anime.safetensors"], {}]}}},
    "UNETLoader": {"input": {"required": {"unet_name": [["real.safetensors"]], "weight_dtype": [["default"], {}]}}},
    "EmptyLatentImage": {"input": {"required": {}}},
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {}], "clip": ["CLIP"]}}},
    "VAEDecode": {"input": {"required": {}}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
    # An upload widget's declared type is the *combo* of files already in the
    # server's input directory, so no string-kind comparison can find it; the
    # `image_upload` flag is the typing rule.
    "LoadImage": {
        "input": {
            "required": {"image": [["a.png", "b.png"], {"image_upload": True}]},
            "optional": {"channel": [["red", "green"], {}]},
        }
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
    "slots": {"positive": ["1", "text"], "negative": ["2", "text"], "seed": ["3", "seed"], "output": ["4", "images"]},
}

# An imported graph that loads its diffusion model via UNETLoader and pins a
# filename from the machine that exported the PNG. The `checkpoint` slot marks the
# input Orb's model selection overrides.
UNET_USER_GRAPH = {
    **USER_GRAPH,
    "id": "user_unet",
    "label": "Unet",
    "graph": {
        **USER_GRAPH["graph"],
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "gone.safetensors", "weight_dtype": "default"}},
    },
    "slots": {**USER_GRAPH["slots"], "positive": ["2", "text"], "checkpoint": ["1", "unet_name"]},
}

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


def _install_client(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        ExternalComfyAdapter, "_client", lambda _self: ComfyClient("http://comfy.test", transport=httpx.MockTransport(handler))
    )


def _handler(models_response: httpx.Response, info: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/system_stats":
            return httpx.Response(
                200, json={"system": {"comfyui_version": "0.22.0"}, "devices": [{"name": "RTX 3090", "vram_total": 1}]}
            )
        if request.url.path == "/object_info":
            return httpx.Response(200, json=info if info is not None else OBJECT_INFO)
        if request.url.path == "/models/checkpoints":
            return models_response
        return httpx.Response(404)

    return handler


def _config(**external) -> dict:
    return normalize_config({"external_comfy": external})


# ── test connection ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (httpx.Response(200, json=["z.safetensors", "anime.safetensors"]), ["anime.safetensors", "z.safetensors"]),
        # Discovery only fills the settings dropdown, so a server that validated
        # every selected graph is connected whether or not it lists models.
        (httpx.Response(403), []),
    ],
    ids=["discovered", "listing refused"],
)
async def test_connection_test_reports_whatever_checkpoints_it_could_discover(monkeypatch, models, expected):
    _install_client(monkeypatch, _handler(models))
    config = _config(styles=[{"id": "s", "label": "S", "checkpoint": "anime.safetensors"}])

    result = await ExternalComfyAdapter(config).validate_connection()

    assert result["ok"] is True
    assert result["models"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("overridden", [True, False], ids=["override validated", "graph's own pin surfaced"])
async def test_validation_checks_the_model_that_will_actually_run(monkeypatch, overridden):
    """The graph pins "gone.safetensors" from another machine. With a checkpoint
    slot, the user's Orb selection overrides it and validation must check that; with
    no slot the graph keeps its own missing model, and Test connection says so
    rather than letting it fail cryptically mid-render."""
    _install_client(monkeypatch, _handler(httpx.Response(200, json=["real.safetensors"])))
    graph = (
        UNET_USER_GRAPH
        if overridden
        else {**UNET_USER_GRAPH, "slots": {k: v for k, v in UNET_USER_GRAPH["slots"].items() if k != "checkpoint"}}
    )
    style = {"id": "s", "label": "S", "workflow": "user_unet", "checkpoint": "real.safetensors" if overridden else ""}
    config = _config(user_graphs=[graph], styles=[style])

    if overridden:
        assert (await ExternalComfyAdapter(config).validate_connection())["ok"] is True
    else:
        with pytest.raises(ImageGenerationError, match="no longer available"):
            await ExternalComfyAdapter(config).validate_connection()


@pytest.mark.asyncio
async def test_object_info_is_cached_for_probes_and_refetched_on_an_explicit_test(monkeypatch):
    calls = {"object_info": 0}
    inner = _handler(httpx.Response(200, json=["anime.safetensors"]))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/object_info":
            calls["object_info"] += 1
        return inner(request)

    _install_client(monkeypatch, handler)
    invalidate_object_info()
    config = _config(styles=[{"id": "s", "label": "S", "checkpoint": "anime.safetensors"}])

    await ExternalComfyAdapter(config).validate_connection(allow_cached=True)
    await ExternalComfyAdapter(config).validate_connection(allow_cached=True)
    assert calls["object_info"] == 1, "the readiness probe must not refetch the node catalogue every modal open"

    # Pressing Test connection means "look again".
    await ExternalComfyAdapter(config).validate_connection(allow_cached=False)
    assert calls["object_info"] == 2


# ── slot typing for the importer ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_node_roles_type_slots_from_object_info_and_skip_unknown_classes(monkeypatch):
    """The picker's typing crosses the wire as a verdict, not as /object_info: a real
    install reports ~2000 node types, tens of megabytes, and handing that to a
    browser to populate four dropdowns is what this route exists to avoid."""
    _install_client(monkeypatch, _handler(httpx.Response(404)))
    invalidate_object_info()
    roles = await ExternalComfyAdapter(normalize_config({})).node_roles(
        ["CLIPTextEncode", "KSampler", "SaveImage", "LoadImage", "CheckpointLoaderSimple", "Nope"]
    )

    assert roles["CLIPTextEncode"]["text_inputs"] == ["text"]
    assert roles["KSampler"]["seed_inputs"] == ["seed"]  # `steps` is an INT too
    assert roles["SaveImage"]["output_node"] is True
    assert roles["LoadImage"]["image_inputs"] == ["image"]
    # A plain combo (`channel`, `ckpt_name`) is a fixed menu, not an upload slot.
    assert roles["CheckpointLoaderSimple"]["image_inputs"] == []
    assert "Nope" not in roles


# ── generation ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_uploads_each_reference_once_and_patches_the_widget(monkeypatch):
    uploads: list[str] = []
    submitted: dict = {}
    responses = {
        "/prompt": httpx.Response(200, json={"prompt_id": "p1", "number": 1}),
        "/queue": httpx.Response(200, json={"queue_running": [], "queue_pending": []}),
        "/history/p1": httpx.Response(
            200,
            json={
                "p1": {
                    "status": {"completed": True},
                    "outputs": {"4": {"images": [{"filename": "x.png", "subfolder": "", "type": "output"}]}},
                }
            },
        ),
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
    config = _config(user_graphs=[EDIT_USER_GRAPH], styles=[{"id": "s", "label": "S", "workflow": "user_edit"}])
    reference = ResolvedReference(
        slot=("0", "image"),
        source="character",
        data=b"\x89PNG\r\n\x1a\n" + b"ref",
        mime="image/png",
        origin="character:card-1",
        digest="deadbeefdeadbeef" + "0" * 48,
    )
    # Two slots, one image: the adapter must upload once.
    request = ImageRequest(
        prompt="p", negative_prompt="", seed=1, style_id="s", timeout_seconds=5, references=(reference, reference)
    )

    adapter = ExternalComfyAdapter(config)
    result = await adapter.generate(request, target=adapter.resolve_target(resolve_style(config, "s"), None))

    assert len(uploads) == 1
    assert submitted["prompt"]["0"]["inputs"]["image"] == "orb/orb_deadbeefdeadbeef.png"
    # The replay record names where the bytes came from, not just what was sent.
    assert result.backend_info["references"][0]["origin"] == "character:card-1"
    assert result.backend_info["references"][0]["comfy_name"] == "orb/orb_deadbeefdeadbeef.png"
