"""API-format ComfyUI graph loading, validation, and explicit slot patching."""

from __future__ import annotations

import copy
import json
from importlib.resources import files
from typing import Any, Mapping

from .contracts import ImageGenerationError

CORE_SLOTS = {
    "positive": ["6", "text"],
    "negative": ["7", "text"],
    "seed": ["3", "seed"],
    "checkpoint": ["4", "ckpt_name"],
    "output": ["9", "images"],
}


def load_core_graph() -> dict:
    resource = files(__package__).joinpath("resources/workflows/external_core.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def resolve_graph(config: Mapping[str, Any], graph_id: str) -> tuple[dict, dict, bool]:
    if graph_id == "external_core":
        return load_core_graph(), copy.deepcopy(CORE_SLOTS), True
    for item in config["external_comfy"]["user_graphs"]:
        if item["id"] == graph_id:
            return copy.deepcopy(item["graph"]), copy.deepcopy(item["slots"]), False
    raise ImageGenerationError(f"Configured workflow {graph_id!r} no longer exists")


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
        inputs, name = _input_slot(patched, slots.get(role), role)
        inputs[name] = value
    if "checkpoint" in slots:
        inputs, name = _input_slot(patched, slots["checkpoint"], "checkpoint")
        if not checkpoint:
            raise ImageGenerationError("Select a checkpoint before generating")
        inputs[name] = checkpoint
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
