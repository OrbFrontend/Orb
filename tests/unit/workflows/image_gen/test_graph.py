from __future__ import annotations

import pytest

from backend.workflows.image_gen.engine.contracts import ImageGenerationError
from backend.workflows.image_gen.engine.graph import (
    describe_render_params,
    enabled_references,
    fit_seed,
    patch_graph,
    seed_input,
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


# ── the optional size slots ──────────────────────────────────────────────────
#
# Optional for the same reason `negative` is: an img2img graph takes its size from
# the reference or an aspect-ratio node, and there is no width/height pair to write.
# A graph that maps neither must behave precisely as it did before the slot existed.

SIZED_SLOTS = {**CORE_SLOTS, "width": ["5", "width"], "height": ["5", "height"]}


def test_mapped_size_slots_are_patched_and_unmapped_ones_are_left_alone():
    patched, _ = patch_graph(
        _base_graph(),
        SIZED_SLOTS,
        prompt="p",
        negative_prompt="n",
        seed=1,
        checkpoint="model.safetensors",
        width=1024,
        height=1536,
    )
    assert (patched["5"]["inputs"]["width"], patched["5"]["inputs"]["height"]) == (1024, 1536)

    # The same graph with no size slots mapped: the arguments are simply not written.
    untouched, _ = patch_graph(
        _base_graph(),
        CORE_SLOTS,
        prompt="p",
        negative_prompt="n",
        seed=1,
        checkpoint="model.safetensors",
        width=1024,
        height=1536,
    )
    assert (untouched["5"]["inputs"]["width"], untouched["5"]["inputs"]["height"]) == (1024, 1024)


def test_a_mapped_size_slot_is_skipped_when_no_size_was_asked_for():
    """`validate_connection` patches to check the model and passes no size, and a
    style whose graph gained a slot after the fact has none resolved yet. Writing a
    JSON null into an INT widget would fail at ComfyUI rather than here."""
    patched, _ = patch_graph(
        _base_graph(), SIZED_SLOTS, prompt="p", negative_prompt="n", seed=1, checkpoint="model.safetensors"
    )
    assert patched["5"]["inputs"]["width"] == 1024


def test_size_slots_are_validated_when_present_and_ignored_when_not():
    graph, _ = _core()
    validate_graph_structure(graph, SIZED_SLOTS, OBJECT_INFO)
    # A dangling one is caught at Test connection rather than mid-render.
    with pytest.raises(ImageGenerationError, match="width slot"):
        validate_graph_structure(graph, {**SIZED_SLOTS, "width": ["999", "width"]}, OBJECT_INFO)
    validate_graph_structure(*_core(), OBJECT_INFO)


# ── structural validation against a server's /object_info ────────────────────
# All render-free: `/prompt` has no dry-run, so a submission that validates
# executes, and preflighting by submitting would spend a full render per save.


def test_a_valid_graph_passes_structural_validation():
    validate_graph_structure(*_core(), OBJECT_INFO)


@pytest.mark.parametrize(
    ("break_it", "match"),
    [
        (lambda g, s: g["6"].__setitem__("class_type", "SomeCustomTextEncode"), "SomeCustomTextEncode"),
        (lambda g, s: g["4"]["inputs"].__setitem__("ckpt_name", "deleted.safetensors"), "no longer available"),
        (lambda g, s: s.__setitem__("positive", ["6", "prompt_text"]), "positive slot"),
        # VAEDecode: a real node, but it saves nothing.
        (lambda g, s: s.__setitem__("output", ["8", "images"]), "does not save or preview"),
        (lambda g, s: s.__setitem__("output", ["999", "images"]), "no configured output node"),
    ],
    ids=["unknown node type", "stale combo value", "slot on a missing input", "output saves nothing", "output node absent"],
)
def test_validation_names_what_this_server_cannot_run(break_it, match):
    graph, slots = _core()
    break_it(graph, slots)
    with pytest.raises(ImageGenerationError, match=match):
        validate_graph_structure(graph, slots, OBJECT_INFO)


# ── reference images ─────────────────────────────────────────────────────────


def _with_reference():
    """The core graph plus a LoadImage carrying a filename from another machine."""
    graph, slots = _core()
    graph["11"] = {"class_type": "LoadImage", "inputs": {"image": "woman-in-black.jpeg"}}
    slots["references"] = [{"slot": ["11", "image"], "label": "Load Image (#11)"}]
    return graph, slots


def _filled(slots):
    """Every declared slot, as a style that switched its reference on passes them."""
    return enabled_references(slots, "character")


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


def test_a_filled_reference_is_exempt_from_the_combo_membership_check():
    """The widget value is replaced per render with a file this server does not
    have yet, so its membership in the input-directory listing means nothing.
    Without the exemption, Test connection rejects every edit workflow."""
    graph, slots = _with_reference()
    validate_graph_structure(graph, slots, OBJECT_INFO, filled=_filled(slots))


def test_a_declared_but_switched_off_slot_still_has_to_name_a_file_that_is_there():
    """The exemption tracks what Orb will *overwrite*, not what the graph declares.

    A style that leaves a slot off renders the filename the workflow was exported
    with, so a stale one is as fatal as it was before the slot was declared at all --
    and saying so at Test connection is the only place it is cheap to find out.
    """
    graph, slots = _with_reference()
    with pytest.raises(ImageGenerationError, match="point this style's reference image at it"):
        validate_graph_structure(graph, slots, OBJECT_INFO, filled=enabled_references(slots, ""))


def test_an_undeclared_image_input_says_how_to_fix_it():
    # "no longer available on this server" reads as a broken install; for a
    # filename the actionable answer is to upload it there or fill the slot.
    graph, slots = _with_reference()
    slots.pop("references")
    with pytest.raises(ImageGenerationError, match="point this style's reference image at it"):
        validate_graph_structure(graph, slots, OBJECT_INFO)


def test_a_dangling_reference_slot_is_caught_at_test_connection():
    """Otherwise it only surfaces mid-render, after the upload and a queue wait.

    Checked against the *declared* list rather than the filled one: a slot naming a
    node that is gone is a broken graph whichever style is looking at it, and one that
    only failed once someone switched it on would be found by the wrong person.
    """
    graph, slots = _with_reference()
    slots["references"].append({"slot": ["999", "image"], "label": "Gone"})
    with pytest.raises(ImageGenerationError, match="reference image slot points to a missing node"):
        validate_graph_structure(graph, slots, OBJECT_INFO, filled=_filled(slots))


def test_one_source_switches_every_declared_slot_on_or_none_of_them():
    """The source policy enables the whole declared list. Which character fills each
    slot is planned later from the round, so no per-slot setting can drift out of
    alignment with the graph."""
    _, slots = _with_reference()
    slots["references"].append({"slot": ["12", "image"], "label": "Load Image (#12)"})

    assert [entry["slot"] for entry in enabled_references(slots, "previous")] == [["11", "image"], ["12", "image"]]
    # Off is off for the whole graph: every `Load Image` keeps the file it was
    # exported with, and nothing about the conversation is uploaded.
    assert enabled_references(slots, "") == []


def test_render_params_are_read_back_off_the_graph_that_executes():
    graph, slots = _core()
    params = describe_render_params(graph, slots)
    assert params == {
        "width": 1024,
        "height": 1024,
        # False because `_core()` maps no size slots, so the pair above came from the
        # scan. The value is still recorded -- it is a best-effort record -- but it is
        # graded, so a consumer that shows a size as fact can decline this one.
        "size_measured": False,
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


def test_the_mapped_size_slots_win_over_the_positional_scan():
    """The scan takes the first node in sorted order carrying a width/height pair,
    which need not be the node Orb patched -- an upscale node can sort first. Already
    imprecise; wrong in a new way once Orb writes to one of them, because the record
    would then name a size the render did not use.
    """
    graph, slots = _core()
    # Sorts before the EmptyLatentImage at "5", and is not what Orb patches.
    graph["2"] = {"class_type": "ImageScale", "inputs": {"width": 512, "height": 512}}
    scanned = describe_render_params(graph, slots)
    assert scanned["width"] == 512, "precondition: the scan picks the wrong node"
    # And says so, which is the whole reason the flag exists: this number reaches a
    # user-facing "Size" row, and one guessed off an upscale node must not be shown
    # as what the image was rendered at.
    assert scanned["size_measured"] is False

    sized = describe_render_params(graph, {**slots, "width": ["5", "width"], "height": ["5", "height"]})
    assert (sized["width"], sized["height"]) == (1024, 1024)
    assert sized["size_measured"] is True, "the mapped slots name the node Orb wrote to"
    # A slot pointing at a node that is gone falls back to the scan rather than
    # reporting nothing: a best-effort record degrades, it does not fail. It degrades
    # to an *ungraded* answer too, or the fallback would inherit the mapping's credit.
    dangling = describe_render_params(graph, {**slots, "width": ["999", "width"], "height": ["999", "height"]})
    assert dangling["width"] == 512
    assert dangling["size_measured"] is False


# A custom seed node that stops far short of the sampler's 2**64. rgthree's is the
# one users hit, because ComfyUI rejects the whole prompt over it -- "Value
# 18257206749444865874 bigger than max of 1125899906842624" -- naming a number
# nobody typed.
RGTHREE_SEED = {"input": {"required": {"seed": ["INT", {"default": 0, "min": -(2**50), "max": 2**50}]}}}


def test_the_seed_slot_names_the_class_that_declares_its_bound():
    graph, slots = _core()
    assert seed_input(graph, slots) == ("KSampler", "seed")
    # Read-only: a slot that no longer resolves is "no answer", not an error, because
    # the render it degrades to is the one Orb submitted before it started asking.
    assert seed_input(graph, {**slots, "seed": ["999", "seed"]}) is None
    assert seed_input(graph, {**slots, "seed": None}) is None


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        (18257206749444865874, 739759991701499),
        # Inside the range already: untouched, so the common case records the seed
        # the user sees on the image.
        (12345, 12345),
        (2**50, 2**50),
        (2**50 + 1, 0),
    ],
    ids=["oversized", "in range", "the boundary itself", "one past it"],
)
def test_an_oversized_seed_is_folded_into_what_the_node_accepts(seed, expected):
    assert fit_seed(seed, RGTHREE_SEED, "seed") == expected
    # Idempotent, which is what lets Orb record the folded seed and still reproduce
    # this render when it is replayed.
    assert fit_seed(expected, RGTHREE_SEED, "seed") == expected


def test_the_fold_stays_off_the_negative_half_a_node_offers():
    """rgthree declares min=-2**50. Folding across the whole declared span would hand
    ComfyUI a legal but negative seed for a seed the user never sees as negative."""
    assert 0 <= fit_seed(18257206749444865874, RGTHREE_SEED, "seed") <= 2**50


@pytest.mark.parametrize(
    ("info", "name"),
    [
        (OBJECT_INFO["KSampler"], "seed"),  # declares no bound at all
        ({}, "seed"),  # a class this server does not know
        (RGTHREE_SEED, "noise_seed"),  # the slot names an input this class does not declare
        ({"input": {"required": {"seed": ["INT", {"min": 10, "max": 1}]}}}, "seed"),  # inverted
        ({"input": {"required": {"seed": ["INT", {"min": 0, "max": "big"}]}}}, "seed"),  # not a number
    ],
    ids=["unbounded", "unknown class", "undeclared input", "inverted range", "non-numeric bound"],
)
def test_a_node_that_declares_no_usable_bound_leaves_the_seed_alone(info, name):
    assert fit_seed(2**63, info, name) == 2**63
