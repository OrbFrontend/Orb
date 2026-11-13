"""Standalone pictures compose once from a description, style and optional saved likeness."""

import base64
import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.database import create_character_card, get_settings
from backend.workflows import StandaloneCtx, set_workflow_character_state, set_workflow_config
from backend.workflows.image_gen import composer
from backend.workflows.image_gen.engine import ImageResult
from backend.workflows.image_gen.engine.contracts import RenderTarget
from backend.workflows.image_gen.standalone import standalone


@pytest.mark.parametrize("prompt_format", ["tags", "hybrid", "prose"])
async def test_standalone_composes_once_with_default_style_and_character_reference(client, db, monkeypatch, prompt_format):
    await create_character_card({"id": "iris", "name": "Iris"})
    image = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(image, "PNG")
    await set_workflow_character_state(
        "iris",
        "image_gen",
        {
            "appearance_prompt": "silver hair",
            "negative_prompt": "glasses",
            "reference_image_b64": base64.b64encode(image.getvalue()).decode(),
            "reference_mime": "image/png",
        },
    )
    await set_workflow_config(
        "image_gen",
        {
            "default_style": "portrait",
            "styles": [
                {
                    "id": "portrait",
                    "label": "Portrait",
                    "prompt_format": prompt_format,
                    "prompt": "{{char}} portrait style",
                    "negative_prompt": "blurry",
                    "reference_source": "previous",
                }
            ],
            "scene_skills_enabled": True,
            "pov_mode": "first",
            "refine_turns": 5,
        },
    )
    target = RenderTarget(
        source="cloud",
        target_id="",
        model="image-model",
        supports_negative_prompt=True,
        supports_seed=True,
        supports_dimensions=True,
        width=1024,
        height=1024,
        reference_capacity=1,
        reference_template={"slot_prefix": "reference", "required": False},
    )
    selected, calls, renders, phases = [], [], [], []

    def adapter(config, style):
        selected.append(style)
        return SimpleNamespace(resolve_target=lambda replay: replace(target, reference_source=style["reference_source"]))

    async def forced(**kwargs):
        calls.append(kwargs)
        yield {
            "type": "result",
            "args": {
                "scene": "Iris wears a blue coat." if prompt_format == "prose" else "1girl, solo, blue coat",
                "unwanted": "hat",
                "visible_subjects": ["Iris"],
            },
        }

    async def render(adapter, request, **kwargs):
        renders.append(request)
        kwargs["progress"]("rendering", {"backend": "test"})
        return ImageResult(b"rendered", "image/png", {})

    monkeypatch.setattr("backend.workflows.image_gen.standalone.get_adapter", adapter)
    monkeypatch.setattr("backend.workflows.image_gen.standalone.resolve_and_generate", render)
    monkeypatch.setattr(composer, "forced_tool_call", forced)
    result = await standalone(
        StandaloneCtx(
            description="An avatar of Iris in a blue coat.",
            settings=await get_settings(),
            agent_client=object(),
            agent_model_name="agent",
            character_id="iris",
            character={"name": "Iris"},
            phase=phases.append,
        )
    )
    assert result.data == b"rendered" and result.mime == "image/png"
    # Review turns are on, so the Agent model is one that takes pictures: it is handed this one to look at.
    assert result.preview_url.startswith("data:image/")
    assert selected[0]["reference_source"] == "character", "a style that uses references draws the card's, with no chat image"
    assert len(calls) == len(renders) == 1
    assert selected[0]["id"] == "portrait" and selected[0]["prompt_format"] == prompt_format
    assert calls[0]["prefix"] == [] and calls[0]["offer_tools"] == ("compose_image_prompt",)
    instruction = calls[0]["tail_messages"][0]["content"]
    assert "An avatar of Iris in a blue coat." in instruction
    assert "silver hair" in instruction and "Iris portrait style" in instruction
    assert "previous assistant reply" not in instruction
    request = renders[0]
    assert "silver hair" in request.prompt and "Iris portrait style" in request.prompt
    assert "glasses" in request.negative_prompt and "blurry" in request.negative_prompt
    assert len(request.references) == 1 and request.references[0].origin == "character:iris"
    assert request.style_id == "portrait"
    assert phases == ["Composing the image prompt…", "Rendering on test..."]


@pytest.mark.parametrize("accepts_references", [True, False])
async def test_explicit_reference_enables_style_inputs_or_refuses_before_composing(client, db, monkeypatch, accepts_references):
    from backend.workflows import StandaloneReference
    from backend.workflows.image_gen.engine import ImageGenerationError
    from tests.integration.workflows.image_gen.test_hooks import CLOUD_CONFIG

    await set_workflow_config("image_gen", CLOUD_CONFIG)
    image = io.BytesIO()
    Image.new("RGB", (24, 24), "green").save(image, "PNG")
    supplied = image.getvalue()
    calls, rendered = [], []

    async def forced(**kwargs):
        calls.append(kwargs)
        yield {"type": "result", "args": {"scene": "a green portrait", "unwanted": "", "visible_subjects": []}}

    async def render(adapter, request, **kwargs):
        rendered.append((adapter, request))
        return ImageResult(supplied, "image/png", {})

    monkeypatch.setattr(composer, "forced_tool_call", forced)
    monkeypatch.setattr("backend.workflows.image_gen.standalone.resolve_and_generate", render)
    if not accepts_references:
        monkeypatch.setattr(
            "backend.workflows.image_gen.standalone.get_adapter",
            lambda *_: SimpleNamespace(
                resolve_target=lambda _: RenderTarget(
                    source="cloud",
                    target_id="",
                    model="text-only",
                    supports_negative_prompt=False,
                    supports_seed=False,
                    supports_dimensions=False,
                    width=None,
                    height=None,
                )
            ),
        )
    ctx = StandaloneCtx(
        description="The same picture in green.",
        settings=await get_settings(),
        agent_client=object(),
        agent_model_name="agent",
        reference=StandaloneReference(supplied, "image/png", "assistant:earlier"),
    )
    if not accepts_references:
        with pytest.raises(ImageGenerationError, match="no reference image inputs"):
            await standalone(ctx)
        assert not calls and not rendered
        return
    result = await standalone(ctx)
    adapter, request = rendered[0]
    assert adapter.style["reference_source"] == "previous"
    assert len(request.references) == 1 and request.references[0].origin == "assistant:earlier"
    # Nothing in this config says the Agent model takes pictures, and one that does not would fail every call replaying it.
    assert len(calls) == 1 and result.preview_url == ""
