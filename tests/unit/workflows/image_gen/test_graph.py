from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine.contracts import ImageGenerationError
from backend.workflows.image_gen.engine.graph import (
    describe_render_params,
    patch_graph,
    validate_graph_structure,
)

# A generic SDXL-shaped graph standing in for a typical imported workflow, with
# the standard slot map Orb patches through. External mode ships no default
# graph, so these fixtures live in the test rather than being loaded from one.
CORE_SLOTS = {
    "positive": ["6", "text"],
    "negative": ["7", "text"],
    "seed": ["3", "seed"],
    "checkpoint": ["4", "ckpt_name"],
    "output": ["9", "images"],
}


def _base_graph() -> dict:
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 0,
                "steps": 24,
                "cfg": 6.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ""}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "Orb", "images": ["8", 0]}},
    }


# A minimal /object_info shaped like the real one: `input.required` maps an input
# name to [type, options], where a list type is a combo of legal values.
OBJECT_INFO = {
    "CLIPTextEncode": {"input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}}},
    "KSampler": {
        "input": {
            "required": {
                "seed": ["INT", {}],
                "steps": ["INT", {}],
                "sampler_name": [["euler", "dpmpp_2m"], {}],
            }
        }
    },
    "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["model.safetensors"], {}]}}},
    "EmptyLatentImage": {"input": {"required": {"width": ["INT", {}], "height": ["INT", {}]}}},
    "VAEDecode": {"input": {"required": {}}},
    "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
    # The real shape of an upload widget: a combo of the server's input directory
    # plus the `image_upload` flag that types it as a reference slot.
    "LoadImage": {"input": {"required": {"image": [["already-there.png"], {"image_upload": True}]}}},
}


def _core():
    """A representative imported graph with a checkpoint filled in, as a real submission has."""
    graph = _base_graph()
    graph["4"]["inputs"]["ckpt_name"] = "model.safetensors"
    return graph, dict(CORE_SLOTS)


def test_graph_patches_only_declared_slots():
    original = _base_graph()
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


def test_graph_requires_checkpoint():
    with pytest.raises(ImageGenerationError, match="checkpoint"):
        patch_graph(
            _base_graph(),
            CORE_SLOTS,
            prompt="x",
            negative_prompt="",
            seed=1,
            checkpoint="",
        )


def test_patch_neutralizes_prompt_wired_filenames():
    """Imported graphs that name files by prompt (a literal or a link into
    filename_prefix) would overflow the OS filename limit on long scene prompts."""
    graph, slots = _core()
    graph["9"]["inputs"]["filename_prefix"] = "x" * 300  # a literal too-long prefix
    graph["10"] = {"class_type": "SaveImage", "inputs": {"filename_prefix": ["6", 0], "images": ["8", 0]}}
    patched, _ = patch_graph(graph, slots, prompt="p", negative_prompt="n", seed=1, checkpoint="model.safetensors")
    assert patched["9"]["inputs"]["filename_prefix"] == "orb"
    assert patched["10"]["inputs"]["filename_prefix"] == "orb"  # link clobbered too


def test_a_graph_without_a_negative_slot_still_patches():
    """A prose-trained graph has one text encoder and no negative conditioning."""
    graph, slots = _core()
    del graph["7"]
    slots.pop("negative")
    patched, output = patch_graph(
        graph,
        slots,
        prompt="a quiet room",
        negative_prompt="ignored",
        seed=7,
        checkpoint="model.safetensors",
    )
    assert output == "9"
    assert patched["6"]["inputs"]["text"] == "a quiet room"
    assert "7" not in patched


# ── structural validation against a server's /object_info ────────────────────
# All render-free: `/prompt` has no dry-run, so a submission that validates
# executes, and preflighting by submitting would spend a full render per save.


def test_a_valid_graph_passes_structural_validation():
    graph, slots = _core()
    validate_graph_structure(graph, slots, OBJECT_INFO)


def test_validation_names_a_node_type_this_server_lacks():
    graph, slots = _core()
    graph["6"]["class_type"] = "SomeCustomTextEncode"
    with pytest.raises(ImageGenerationError, match="SomeCustomTextEncode"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_validation_catches_a_combo_value_the_server_does_not_offer():
    graph, slots = _core()
    graph["4"]["inputs"]["ckpt_name"] = "deleted.safetensors"
    with pytest.raises(ImageGenerationError, match="no longer available"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_validation_catches_a_slot_pointing_at_a_missing_input():
    graph, slots = _core()
    slots["positive"] = ["6", "prompt_text"]
    with pytest.raises(ImageGenerationError, match="positive slot"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_validation_requires_an_output_node():
    graph, slots = _core()
    slots["output"] = ["8", "images"]  # VAEDecode: real node, but saves nothing
    with pytest.raises(ImageGenerationError, match="does not save or preview"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_validation_requires_the_output_slot_to_name_a_present_node():
    graph, slots = _core()
    slots["output"] = ["999", "images"]
    with pytest.raises(ImageGenerationError, match="no configured output node"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


# ── reference images ─────────────────────────────────────────────────────────


def _with_reference():
    """The core graph plus a LoadImage carrying a filename from another machine."""
    graph, slots = _core()
    graph["11"] = {"class_type": "LoadImage", "inputs": {"image": "woman-in-black.jpeg"}}
    slots["references"] = [{"slot": ["11", "image"], "source": "character", "label": "Load Image (#11)"}]
    return graph, slots


def test_a_reference_slot_is_patched_with_the_uploaded_widget_value():
    graph, slots = _with_reference()
    patched, _ = patch_graph(
        graph,
        slots,
        prompt="p",
        negative_prompt="n",
        seed=1,
        checkpoint="model.safetensors",
        references=[(("11", "image"), "orb/orb_abc123.webp")],
    )
    assert patched["11"]["inputs"]["image"] == "orb/orb_abc123.webp"
    # The original is untouched, as it is for every other patched slot.
    assert graph["11"]["inputs"]["image"] == "woman-in-black.jpeg"


def test_a_mapped_reference_is_exempt_from_the_combo_membership_check():
    """The widget value is replaced per render with a file this server does not
    have yet, so its membership in the input-directory listing means nothing.
    Without the exemption, Test connection rejects every edit workflow."""
    validate_graph_structure(*_with_reference(), OBJECT_INFO)


def test_an_unmapped_image_input_says_how_to_fix_it():
    # "no longer available on this server" reads as a broken install; for a
    # filename the actionable answer is to upload it there or map the slot.
    graph, slots = _with_reference()
    slots.pop("references")
    with pytest.raises(ImageGenerationError, match="map it as a reference image"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_a_dangling_reference_slot_is_caught_at_test_connection():
    """Otherwise it only surfaces mid-render, after the upload and a queue wait."""
    graph, slots = _with_reference()
    slots["references"].append({"slot": ["999", "image"], "source": "character", "label": "Gone"})
    with pytest.raises(ImageGenerationError, match="reference image slot points to a missing node"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_render_params_are_read_back_off_the_graph_that_executes():
    graph, slots = _core()
    params = describe_render_params(graph, slots)
    assert params == {
        "width": 1024,
        "height": 1024,
        "steps": 24,
        "cfg": 6.0,
        "sampler": "euler",
        "scheduler": "normal",
    }


def test_render_params_report_none_for_linked_or_absent_inputs():
    """A wired input has no value until execution, so it is not an identity."""
    graph, slots = _core()
    graph["3"]["inputs"]["steps"] = ["10", 0]
    del graph["5"]
    params = describe_render_params(graph, slots)
    assert params["steps"] is None
    assert params["width"] is None
    assert params["sampler"] == "euler"
