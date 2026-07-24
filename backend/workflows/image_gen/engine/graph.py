"""API-format ComfyUI graph loading, validation, and explicit slot patching."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from .contracts import ImageGenerationError


def resolve_graph(config: Mapping[str, Any], graph_id: str) -> tuple[dict, dict]:
    """The imported graph and its slot map for `graph_id`.

    External mode ships no default graph: every style renders through a workflow
    the user imported, so an empty or dangling id is a configuration gap, not a
    fallback. The messages say which so the caller can surface it verbatim.
    """
    for item in config["external_comfy"]["user_graphs"]:
        if item["id"] == graph_id:
            return copy.deepcopy(item["graph"]), copy.deepcopy(item["slots"])
    if not graph_id:
        raise ImageGenerationError("Import a ComfyUI workflow and assign it to this style before generating")
    raise ImageGenerationError(f"Configured workflow {graph_id!r} no longer exists")


def has_graph(config: Mapping[str, Any], graph_id: str) -> bool:
    """Whether `graph_id` still resolves, without paying for a deep copy.

    Replay asks this before honouring the graph id recorded on a stored image:
    a user graph deleted since the render must degrade to the style's current
    workflow with a note, not raise from inside the adapter.
    """
    return any(item["id"] == graph_id for item in config["external_comfy"]["user_graphs"])


def _scalar(inputs: Mapping[str, Any], name: str, kinds: tuple[type, ...]) -> Any:
    """A widget value, or None when the input is absent or wired from a link.

    ComfyUI encodes a link as `[node_id, slot]`, so anything list-shaped is a
    connection whose value only exists at execution time.
    """
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, kinds):
        return None
    return value


def describe_render_params(graph: Mapping[str, Any], slots: Mapping[str, Any]) -> dict:
    """Best-effort render identity read back off the graph that will execute.

    Recorded on the attachment so a later replay can say what changed. Derived
    from the graph rather than from a catalog entry because external mode has no
    catalog -- a user-imported graph gets the same treatment as the shipped one
    wherever it uses the standard node inputs, and `None` wherever it does not.
    """
    params: dict[str, Any] = dict.fromkeys(("width", "height", "steps", "cfg", "sampler", "scheduler"))
    seed_slot = slots.get("seed")
    sampler_node = graph.get(str(seed_slot[0])) if isinstance(seed_slot, (list, tuple)) and len(seed_slot) == 2 else None
    sampler_inputs = sampler_node.get("inputs") if isinstance(sampler_node, Mapping) else None
    if isinstance(sampler_inputs, Mapping):
        params["steps"] = _scalar(sampler_inputs, "steps", (int,))
        params["cfg"] = _scalar(sampler_inputs, "cfg", (int, float))
        params["sampler"] = _scalar(sampler_inputs, "sampler_name", (str,))
        params["scheduler"] = _scalar(sampler_inputs, "scheduler", (str,))
    # Dimensions live on whichever latent/resize node carries them; node ids are
    # strings of ints in practice, so sort numerically where possible to keep the
    # choice stable across runs rather than dict-order dependent.
    for node_id in sorted(graph, key=lambda k: (len(str(k)), str(k))):
        node = graph[node_id]
        inputs = node.get("inputs") if isinstance(node, Mapping) else None
        if not isinstance(inputs, Mapping):
            continue
        width, height = _scalar(inputs, "width", (int,)), _scalar(inputs, "height", (int,))
        if width and height:
            params["width"], params["height"] = width, height
            break
    return params


def _input_slot(graph: Mapping[str, Any], slot: Any, role: str) -> tuple[dict, str]:
    if not isinstance(slot, (list, tuple)) or len(slot) != 2:
        raise ImageGenerationError(f"The {role} slot is invalid")
    node_id, input_name = str(slot[0]), slot[1]
    node = graph.get(node_id)
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict) or input_name not in node["inputs"]:
        raise ImageGenerationError(f"The {role} slot points to a missing node input")
    return node["inputs"], input_name


def patch_graph(
    graph: Mapping[str, Any],
    slots: Mapping[str, Any],
    *,
    prompt: str,
    negative_prompt: str,
    seed: int,
    checkpoint: str,
) -> tuple[dict, str]:
    patched = copy.deepcopy(dict(graph))
    for role, value in (
        ("positive", prompt),
        ("negative", negative_prompt),
        ("seed", seed),
    ):
        # `negative` is optional: a prose-trained graph (Flux, SD3) legitimately
        # has one text encoder and no negative conditioning to patch. The other
        # two roles are what "render this prompt with this seed" means.
        if role == "negative" and "negative" not in slots:
            continue
        inputs, name = _input_slot(patched, slots.get(role), role)
        inputs[name] = value
    if "checkpoint" in slots:
        inputs, name = _input_slot(patched, slots["checkpoint"], "checkpoint")
        if not checkpoint:
            raise ImageGenerationError("Select a checkpoint before generating")
        inputs[name] = checkpoint
    # Imported graphs often wire the prompt text into a save node's filename_prefix
    # ("name files by prompt"). Orb's long scene prompts overflow the OS 255-byte
    # filename limit (OSError 36). Orb fetches the image by the name ComfyUI reports,
    # never by this prefix, so pinning it to a constant is always safe.
    for node in patched.values():
        node_inputs = node.get("inputs") if isinstance(node, Mapping) else None
        if isinstance(node_inputs, dict) and "filename_prefix" in node_inputs:
            node_inputs["filename_prefix"] = "orb"
    output = slots.get("output")
    if not isinstance(output, (list, tuple)) or len(output) != 2 or str(output[0]) not in patched:
        raise ImageGenerationError("The output slot points to a missing node")
    return patched, str(output[0])


def validate_graph_structure(graph: Mapping[str, Any], slots: Mapping[str, Any], object_info: Mapping[str, Any]) -> None:
    if not graph:
        raise ImageGenerationError("The selected workflow is empty")
    for node_id, node in graph.items():
        if (
            not isinstance(node, Mapping)
            or not isinstance(node.get("class_type"), str)
            or not isinstance(node.get("inputs"), Mapping)
        ):
            raise ImageGenerationError(f"Workflow node {node_id!r} is malformed")
        class_type = node["class_type"]
        info = object_info.get(class_type)
        if not isinstance(info, Mapping):
            raise ImageGenerationError(f"ComfyUI is missing node type {class_type!r}")
        required = info.get("input", {}).get("required", {}) if isinstance(info.get("input"), Mapping) else {}
        optional = info.get("input", {}).get("optional", {}) if isinstance(info.get("input"), Mapping) else {}
        declared = {
            **(required if isinstance(required, Mapping) else {}),
            **(optional if isinstance(optional, Mapping) else {}),
        }
        for name, value in node["inputs"].items():
            spec = declared.get(name)
            if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], list) and not isinstance(value, list):
                if value not in spec[0]:
                    raise ImageGenerationError(f"Node {node_id} input {name!r} is no longer available on this server")
    for role in ("positive", "negative", "seed"):
        if role == "negative" and "negative" not in slots:
            continue
        _input_slot(graph, slots.get(role), role)
    if "checkpoint" in slots:
        _input_slot(graph, slots["checkpoint"], "checkpoint")
    output = slots.get("output")
    if not isinstance(output, (list, tuple)) or len(output) != 2 or str(output[0]) not in graph:
        raise ImageGenerationError("The workflow has no configured output node")
    output_node = graph[str(output[0])]
    output_info = object_info.get(output_node["class_type"])
    if not isinstance(output_info, Mapping) or not output_info.get("output_node"):
        raise ImageGenerationError("The configured output node does not save or preview an image")
