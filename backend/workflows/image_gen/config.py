"""Strict normalization for the image generation workflow configuration."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from .pov import DEFAULT_MODE as DEFAULT_POV_MODE
from .pov import normalize_mode as normalize_pov_mode

WORKFLOW_ID = "image_gen"
MAX_STYLES = 32
MAX_USER_GRAPHS = 32
MAX_GRAPH_BYTES = 512_000
MAX_REFERENCE_SLOTS = 4
# Base64 cap for the per-character reference image: the profile lives on
# `character_cards.workflow_state` and is read on every generate, so an unbounded
# upload is paid for per render. The picker's 10 MB raw cap plus base64's 4/3.
MAX_REFERENCE_IMAGE_B64 = 13_400_000
PROMPT_FORMATS = ("tags", "hybrid", "prose")
DEFAULT_PROMPT_FORMAT = "hybrid"
# Where a mapped `LoadImage` gets its bytes, as an ordered resolution list. The
# combined source is the default so the choice has no cold-start cliff: a slot
# pinned to `previous` alone hard-fails on a new conversation's first Visualize.
REFERENCE_SOURCES: dict[str, tuple[str, ...]] = {
    "previous": ("previous",),
    "character": ("character",),
    "previous_or_character": ("previous", "character"),
}
DEFAULT_REFERENCE_SOURCE = "previous_or_character"

CONFIG_DEFAULTS = {
    "source": "external_comfy",
    "default_style": "realistic",
    "pov_mode": DEFAULT_POV_MODE,
    "scene_analysis": False,
    "prompter_reasoning": False,
    "timeout_seconds": 180.0,
    "external_comfy": {
        "api_url": "http://127.0.0.1:8188",
        "api_key": "",
        "styles": [
            {
                "id": "realistic",
                "label": "Realistic",
                "prompt_format": DEFAULT_PROMPT_FORMAT,
                "prompt": "RAW photo, realistic illumination, realistic shadows, photography, photorealistic, cinematic lighting, detailed skin, high contrast",
                "negative_prompt": "cartoon, anime, drawing, paint, flat, illustration, painting, low detail, low quality, worst quality, bad quality, bad perspective",
                "extra_instructions": "",
                "checkpoint": "",
                "workflow": "",
            },
            {
                "id": "anime",
                "label": "Anime",
                "prompt_format": DEFAULT_PROMPT_FORMAT,
                "prompt": "masterpiece, best quality, anime illustration, very aesthetic, very detailed, high contrast, good perspective",
                "negative_prompt": "photorealistic, pixelated, 3d render, muddy colors, low quality, worst quality, bad quality, score_1, score_2, bad fingers, missing fingers, fused fingers, bad anatomy, bad hair, bad perspective, bad face",
                "extra_instructions": "",
                "checkpoint": "",
                "workflow": "",
            },
        ],
        "user_graphs": [],
    },
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _text(value: Any, limit: int, default: str = "") -> str:
    return value.strip()[:limit] if isinstance(value, str) else default


def _style(raw: Any) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    sid = _text(raw.get("id"), 64)
    if not _ID_RE.fullmatch(sid):
        return None
    prompt_format = _text(raw.get("prompt_format"), 16, DEFAULT_PROMPT_FORMAT).lower()
    if prompt_format not in PROMPT_FORMATS:
        prompt_format = DEFAULT_PROMPT_FORMAT
    # "external_core" was the shipped default graph; it no longer exists. Migrate
    # a config that still names it to unconfigured so the style reads as "assign a
    # workflow", not as a reference to a graph that will never resolve.
    workflow = _text(raw.get("workflow"), 64)
    if workflow == "external_core":
        workflow = ""
    return {
        "id": sid,
        "label": _text(raw.get("label"), 80, sid) or sid,
        "prompt_format": prompt_format,
        "prompt": _text(raw.get("prompt"), 2_000),
        "negative_prompt": _text(raw.get("negative_prompt"), 2_000),
        "extra_instructions": _text(raw.get("extra_instructions"), 2_000),
        "checkpoint": _text(raw.get("checkpoint"), 512),
        "workflow": workflow,
    }


def _slot(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    node, field = value
    if not isinstance(node, (str, int)) or not isinstance(field, str):
        return None
    node_s, field_s = str(node), field.strip()
    if not node_s or len(node_s) > 64 or not field_s or len(field_s) > 128:
        return None
    return [node_s, field_s]


def _reference(raw: Any) -> dict | None:
    """One `LoadImage` widget mapped to a reference source, or None to drop it."""
    if not isinstance(raw, Mapping):
        return None
    slot = _slot(raw.get("slot"))
    source = _text(raw.get("source"), 32)
    if slot is None or source not in REFERENCE_SOURCES:
        return None
    label = _text(raw.get("label"), 120) or f"{slot[0]} — {slot[1]}"
    return {"slot": slot, "source": source, "label": label}


def _strip_machine_local_state(graph: dict) -> dict:
    """A deep copy of `graph` with each node's top-level `is_changed` removed.

    ComfyUI's API export embeds `is_changed` -- for a `LoadImage`, a hash of the
    file on the *exporter's* disk. `IsChangedCache.get` returns a client-supplied
    value verbatim (`execution.py`) as one component of the node's cache signature,
    so a pinned hash masks a change of file *contents at an unchanged path* and the
    render silently returns the previously decoded image (seen on ComfyUI 0.29.0).
    Stripped at import, before the size cap is measured, so machine-local state
    never reaches storage or a submission.
    """
    stripped = copy.deepcopy(graph)
    for node in stripped.values():
        if isinstance(node, dict):
            node.pop("is_changed", None)
    return stripped


def _user_graph(raw: Any) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    gid = _text(raw.get("id"), 64)
    graph = raw.get("graph")
    slots_raw = raw.get("slots")
    if not _ID_RE.fullmatch(gid) or not isinstance(graph, dict) or not isinstance(slots_raw, Mapping):
        return None
    import json

    graph = _strip_machine_local_state(graph)
    if len(json.dumps(graph, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_GRAPH_BYTES:
        return None
    slots: dict[str, Any] = {}
    for name in ("positive", "negative", "seed", "output", "checkpoint"):
        parsed = _slot(slots_raw.get(name))
        if parsed is not None:
            slots[name] = parsed
    # `negative` and `checkpoint` stay optional: a one-encoder prose graph has
    # nothing to map negative to, and a self-contained graph keeps its own model
    # rather than mapping a checkpoint slot for Orb's selection to override.
    if not all(name in slots for name in ("positive", "seed", "output")):
        return None
    # Never required, so a t2i graph normalizes exactly as before: an unmapped
    # LoadImage is simply absent from the list, which is how "Not used" is encoded.
    references_raw = slots_raw.get("references")
    references = [item for item in map(_reference, references_raw) if item] if isinstance(references_raw, list) else []
    if references:
        slots["references"] = references[:MAX_REFERENCE_SLOTS]
    return {
        "id": gid,
        "label": _text(raw.get("label"), 100, gid) or gid,
        "graph": graph,
        "slots": slots,
    }


def normalize_config(raw: Mapping[str, Any] | None) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    external_value = raw.get("external_comfy")
    external_raw: Mapping[str, Any] = external_value if isinstance(external_value, Mapping) else {}
    url = _text(external_raw.get("api_url"), 2_048, CONFIG_DEFAULTS["external_comfy"]["api_url"])
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        url = CONFIG_DEFAULTS["external_comfy"]["api_url"]
    url = url.rstrip("/")

    styles: list[dict] = []
    seen: set[str] = set()
    raw_styles = external_raw.get("styles")
    for candidate in raw_styles if isinstance(raw_styles, list) else CONFIG_DEFAULTS["external_comfy"]["styles"]:
        item = _style(candidate)
        if item and item["id"] not in seen:
            styles.append(item)
            seen.add(item["id"])
        if len(styles) >= MAX_STYLES:
            break
    if not styles:
        styles = copy.deepcopy(CONFIG_DEFAULTS["external_comfy"]["styles"])

    graphs: list[dict] = []
    graph_seen: set[str] = set()
    raw_graphs = external_raw.get("user_graphs")
    for candidate in raw_graphs if isinstance(raw_graphs, list) else []:
        item = _user_graph(candidate)
        if item and item["id"] not in graph_seen:
            graphs.append(item)
            graph_seen.add(item["id"])
        if len(graphs) >= MAX_USER_GRAPHS:
            break

    default_style = _text(raw.get("default_style"), 64, styles[0]["id"])
    if default_style not in {s["id"] for s in styles}:
        default_style = styles[0]["id"]
    try:
        timeout = float(raw.get("timeout_seconds", 180.0))
    except (TypeError, ValueError):
        timeout = 180.0

    return {
        "source": "external_comfy",
        "default_style": default_style,
        "pov_mode": normalize_pov_mode(raw.get("pov_mode")),
        "scene_analysis": bool(raw.get("scene_analysis", False)),
        "prompter_reasoning": raw.get("prompter_reasoning") is True,
        "timeout_seconds": min(900.0, max(10.0, timeout)),
        "external_comfy": {
            "api_url": url,
            "api_key": _text(external_raw.get("api_key"), 2_048),
            "styles": styles,
            "user_graphs": graphs,
        },
    }


def resolve_style(config: Mapping[str, Any], style_id: str) -> dict:
    external = config["external_comfy"]
    style = next((s for s in external["styles"] if s["id"] == style_id), None)
    if style is None:
        raise ValueError(f"unknown image style {style_id!r}")
    # An empty workflow stays empty: external mode has no default graph, so the
    # render path turns "no workflow" into a clear "assign one" error rather than
    # silently substituting.
    return dict(style)


REFERENCE_MIMES = ("image/png", "image/jpeg", "image/webp")


def normalize_profile(raw: Mapping[str, Any] | None) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    # The per-character reference image, for slots resolving to `character`. Dropped
    # rather than truncated when oversized -- half a base64 payload is not a smaller
    # image. Both halves ride together: bytes with no mime cannot be read by ComfyUI.
    image_raw = raw.get("reference_image_b64")
    image = image_raw.strip() if isinstance(image_raw, str) else ""
    mime = _text(raw.get("reference_mime"), 64).lower()
    if len(image) > MAX_REFERENCE_IMAGE_B64 or mime not in REFERENCE_MIMES:
        image, mime = "", ""
    if not image:
        mime = ""
    return {
        "appearance_prompt": _text(raw.get("appearance_prompt"), 2_000),
        "negative_prompt": _text(raw.get("negative_prompt"), 2_000),
        "reference_image_b64": image,
        "reference_mime": mime,
    }
