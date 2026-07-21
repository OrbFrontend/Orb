from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine.contracts import ImageGenerationError
from backend.workflows.image_gen.engine.graph import (
    CORE_SLOTS,
    load_core_graph,
    patch_graph,
)


def test_core_graph_patches_only_declared_slots():
    original = load_core_graph()
    patched, output = patch_graph(
        original,
        CORE_SLOTS,
        prompt="1girl, night",
        negative_prompt="day",
        seed=42,
        checkpoint="model.safetensors",
    )
    assert output == "9"
    assert patched["6"]["inputs"]["text"] == "1girl, night"
    assert patched["7"]["inputs"]["text"] == "day"
    assert patched["3"]["inputs"]["seed"] == 42
    assert patched["4"]["inputs"]["ckpt_name"] == "model.safetensors"
    assert original["4"]["inputs"]["ckpt_name"] == ""


def test_core_graph_requires_checkpoint():
    with pytest.raises(ImageGenerationError, match="checkpoint"):
        patch_graph(
            load_core_graph(),
            CORE_SLOTS,
            prompt="x",
            negative_prompt="",
            seed=1,
            checkpoint="",
        )
