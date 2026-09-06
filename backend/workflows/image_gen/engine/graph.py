"""API-format ComfyUI graph loading, validation, and explicit slot patching."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ImageGenerationError

OPTIONAL_SLOTS = ("negative", "width", "height")


def resolve_graph(config: Mapping[str, Any], graph_id: str) -> tuple[dict, dict]:
    """The imported graph and its slot map for `graph_id`.

    External mode ships no default graph, so an empty or dangling id is a
    configuration gap rather than a fallback; the messages say which, so the caller
    can surface them verbatim.
    """
    for item in config["external_comfy"]["user_graphs"]:
        if item["id"] == graph_id:
            return copy.deepcopy(item["graph"]), copy.deepcopy(item["slots"])
    if not graph_id:
        raise ImageGenerationError("Import a ComfyUI workflow and assign it to this style before generating")
    raise ImageGenerationError(f"Configured workflow {graph_id!r} no longer exists")


def has_graph(config: Mapping[str, Any], graph_id: str) -> bool:
    """Whether `graph_id` still resolves, without paying for a deep copy. Replay
    asks before honouring a stored graph id: one deleted since the render must
    degrade to the style's current workflow with a note, not raise."""
    return any(item["id"] == graph_id for item in config["external_comfy"]["user_graphs"])


def reference_slots(slots: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The image slots this graph declares, as normalization stored them. A graph that
    loads no image has no `references` key at all, so callers treat "not an edit
    workflow" and "no image inputs" as one case.

    Declared, not enabled: whether a slot is actually filled is the rendering style's
    answer, and `enabled_references` is where the two meet.
    """
    entries = slots.get("references")
    return [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []


def enabled_references(slots: Mapping[str, Any], source: str) -> list[Mapping[str, Any]]:
    """The slots this render will fill: all of them, or none.

    One source policy for the whole graph. The render planner assigns separate
    characters to separate `LoadImage` slots in a group round, while reusing the
    primary character for surplus required slots in a smaller scene.

    A style with no source is the same render as the old "Not used": each `LoadImage`
    keeps whatever filename the workflow was exported with, and nothing about the
    conversation is uploaded for it.
    """
    return reference_slots(slots) if source else []


def _scalar(inputs: Mapping[str, Any], name: str, kinds: tuple[type, ...]) -> Any:
    """A widget value, or None when the input is absent or wired from a link.
    ComfyUI encodes a link as `[node_id, slot]`, so anything list-shaped has a
    value only at execution time."""
    value = inputs.get(name)
    if isinstance(value, bool) or not isinstance(value, kinds):
        return None
    return value


def _slot_inputs(graph: Mapping[str, Any], slot: Any) -> Mapping[str, Any] | None:
    """The `inputs` mapping a slot points at, or None when it does not resolve.

    The read-only counterpart of `_input_slot`: describing a graph must degrade to
    "unknown" where patching it would raise.
    """
    if not isinstance(slot, (list, tuple)) or len(slot) != 2:
        return None
    node = graph.get(str(slot[0]))
    inputs = node.get("inputs") if isinstance(node, Mapping) else None
    return inputs if isinstance(inputs, Mapping) else None


def describe_render_params(graph: Mapping[str, Any], slots: Mapping[str, Any]) -> dict:
    """Best-effort render identity read back off the graph that will execute.

    Recorded on the attachment so a later replay can say what changed. Read from the
    graph because external mode has no catalog: a user-imported graph is described
    wherever it uses the standard node inputs, and `None` wherever it does not.

    `size_measured` grades the size this returns, because the two ways it is reached
    are not equally true: the mapped slots name the node Orb wrote to, the fallback
    scan names whichever node sorted first carrying a width/height pair and can pick
    an upscale node over the latent one.
    """
    params: dict[str, Any] = dict.fromkeys(("width", "height", "steps", "cfg", "sampler", "scheduler"))
    params["size_measured"] = False
    seed_slot = slots.get("seed")
    sampler_inputs = _slot_inputs(graph, seed_slot)
    if isinstance(sampler_inputs, Mapping):
        params["steps"] = _scalar(sampler_inputs, "steps", (int,))
        params["cfg"] = _scalar(sampler_inputs, "cfg", (int, float))
        params["sampler"] = _scalar(sampler_inputs, "sampler_name", (str,))
        params["scheduler"] = _scalar(sampler_inputs, "scheduler", (str,))
    mapped = {}
    for role in ("width", "height"):
        inputs = _slot_inputs(graph, slots.get(role))
        mapped[role] = _scalar(inputs, str(slots[role][1]), (int,)) if inputs is not None else None
    if mapped["width"] and mapped["height"]:
        params["width"], params["height"] = mapped["width"], mapped["height"]
        params["size_measured"] = True
        return params
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


def declared_inputs(info: Mapping[str, Any]) -> dict[str, Any]:
    """Every declared input of one `/object_info` node class, required and optional
    alike. Shared by structural validation and the importer's slot typing, so the
    two can never disagree about what a node accepts."""
    spec = info.get("input")
    declared: dict[str, Any] = {}
    for group in ("required", "optional"):
        values = spec.get(group) if isinstance(spec, Mapping) else None
        if isinstance(values, Mapping):
            declared.update(values)
    return declared


def seed_input(graph: Mapping[str, Any], slots: Mapping[str, Any]) -> tuple[str, str] | None:
    """The node class and input name the seed slot points at, or None when it does
    not resolve. Read-only, like `_slot_inputs`: a graph that cannot answer this
    renders with the seed exactly as asked."""
    slot = slots.get("seed")
    if not isinstance(slot, (list, tuple)) or len(slot) != 2:
        return None
    node = graph.get(str(slot[0]))
    class_type = node.get("class_type") if isinstance(node, Mapping) else None
    return (class_type, str(slot[1])) if isinstance(class_type, str) and class_type else None


def fit_seed(seed: int, info: Mapping[str, Any], input_name: str) -> int:
    """`seed` folded into the range the seed node declares, or unchanged where it
    declares none.

    Seed nodes disagree about how large a seed may be -- KSampler takes the whole
    2**64, rgthree's Seed node stops at 2**50 -- and ComfyUI rejects the entire
    prompt over one out-of-range widget, naming a number the user never chose. Each
    node class declares its own bound in `/object_info`, so read it from there rather
    than keeping a list of which nodes are small.

    Folded rather than clamped: clamping would draw every oversized seed as the same
    image, and folding is idempotent, so the seed Orb records still reproduces this
    render.
    """
    spec = declared_inputs(info).get(input_name)
    options = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 else None
    if not isinstance(options, Mapping):
        return seed
    low, high = options.get("min"), options.get("max")
    if any(isinstance(bound, bool) or not isinstance(bound, int) for bound in (low, high)) or high < low:
        return seed
    # Negative seeds are legal where a node offers them and nobody wants them, so the
    # fold starts at zero wherever that is still inside the declared range.
    low = max(low, 0) if high >= 0 else low
    if low <= seed <= high:
        return seed
    return low + (seed - low) % (high - low + 1)


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
    width: int | None = None,
    height: int | None = None,
    references: Sequence[tuple[tuple[str, str], str]] = (),
) -> tuple[dict, str]:
    patched = copy.deepcopy(dict(graph))
    for role, value in (
        ("positive", prompt),
        ("negative", negative_prompt),
        ("seed", seed),
        ("width", width),
        ("height", height),
    ):
        if role in OPTIONAL_SLOTS and (role not in slots or value is None):
            continue
        inputs, name = _input_slot(patched, slots.get(role), role)
        inputs[name] = value
    if "checkpoint" in slots:
        inputs, name = _input_slot(patched, slots["checkpoint"], "checkpoint")
        if not checkpoint:
            raise ImageGenerationError("Select a checkpoint before generating")
        inputs[name] = checkpoint
    for slot, value in references:
        inputs, name = _input_slot(patched, slot, "reference image")
        inputs[name] = value
    for node in patched.values():
        node_inputs = node.get("inputs") if isinstance(node, Mapping) else None
        if isinstance(node_inputs, dict) and "filename_prefix" in node_inputs:
            node_inputs["filename_prefix"] = "orb"
    output = slots.get("output")
    if not isinstance(output, (list, tuple)) or len(output) != 2 or str(output[0]) not in patched:
        raise ImageGenerationError("The output slot points to a missing node")
    return patched, str(output[0])


def is_image_upload(spec: Any) -> bool:
    """Whether an `/object_info` input spec is an upload widget, marked
    ``[[...files...], {"image_upload": true}]``. That flag is the typing rule for a
    reference slot, as a STRING/INT kind is for the others."""
    if not isinstance(spec, (list, tuple)) or len(spec) < 2 or not isinstance(spec[0], list):
        return False
    return isinstance(spec[1], Mapping) and spec[1].get("image_upload") is True


def validate_graph_structure(
    graph: Mapping[str, Any],
    slots: Mapping[str, Any],
    object_info: Mapping[str, Any],
    *,
    filled: Sequence[Mapping[str, Any]] = (),
) -> None:
    """Prove this graph can run here, given the slots a render will actually fill.

    `filled` is the style's *enabled* reference slots, not the graph's declared ones.
    An image widget Orb is about to overwrite may name a file this server has never
    seen; one it will leave alone may not, because that filename is what will render.
    Defaulting to none is the strict reading, so a caller that forgets cannot get the
    exemption by accident.
    """
    if not graph:
        raise ImageGenerationError("The selected workflow is empty")
    mapped = {(str(entry["slot"][0]), str(entry["slot"][1])) for entry in filled if entry.get("slot")}
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
        declared = declared_inputs(info)
        for name, value in node["inputs"].items():
            spec = declared.get(name)
            if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], list) and not isinstance(value, list):
                if (str(node_id), name) in mapped or value in spec[0]:
                    continue
                if is_image_upload(spec):
                    raise ImageGenerationError(
                        f"Node {node_id} needs image {value!r} on the ComfyUI server, "
                        "or point this style's reference image at it"
                    )
                raise ImageGenerationError(f"Node {node_id} input {name!r} is no longer available on this server")
    for role in ("positive", "negative", "seed", "width", "height"):
        if role in OPTIONAL_SLOTS and role not in slots:
            continue
        _input_slot(graph, slots.get(role), role)
    if "checkpoint" in slots:
        _input_slot(graph, slots["checkpoint"], "checkpoint")
    for entry in reference_slots(slots):
        _input_slot(graph, entry.get("slot"), "reference image")
    output = slots.get("output")
    if not isinstance(output, (list, tuple)) or len(output) != 2 or str(output[0]) not in graph:
        raise ImageGenerationError("The workflow has no configured output node")
    output_node = graph[str(output[0])]
    output_info = object_info.get(output_node["class_type"])
    if not isinstance(output_info, Mapping) or not output_info.get("output_node"):
        raise ImageGenerationError("The configured output node does not save or preview an image")
