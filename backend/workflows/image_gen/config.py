"""Strict normalization for the image generation workflow configuration."""

from __future__ import annotations

import copy
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

WORKFLOW_ID = "image_gen"
MAX_STYLES = 32
MAX_USER_GRAPHS = 16
MAX_GRAPH_BYTES = 512_000

STYLE_DEFAULTS = (
    {
        "id": "realistic",
        "label": "Realistic",
        "prompt": "photorealistic, cinematic lighting, detailed skin, high contrast",
        "negative_prompt": "anime, illustration, painting, low detail",
    },
    {
        "id": "anime",
        "label": "Anime",
        "prompt": "anime illustration, clean line art, very aesthetic, high contrast",
        "negative_prompt": "photorealistic, 3d render, muddy colors",
    },
)

CONFIG_DEFAULTS = {
    "source": "external_comfy",
    "default_style": "realistic",
    "scene_analysis": False,
    "timeout_seconds": 180.0,
    "external_comfy": {
        "api_url": "http://127.0.0.1:8188",
        "api_key": "",
        "checkpoint": "",
        "workflow": "external_core",
        "styles": [
            {
                "id": s["id"],
                "label": s["label"],
                "prompt": "",
                "negative_prompt": "",
                "checkpoint": "",
                "workflow": "",
            }
            for s in STYLE_DEFAULTS
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
    return {
        "id": sid,
        "label": _text(raw.get("label"), 80, sid) or sid,
        "prompt": _text(raw.get("prompt"), 2_000),
        "negative_prompt": _text(raw.get("negative_prompt"), 2_000),
        "checkpoint": _text(raw.get("checkpoint"), 512),
        "workflow": _text(raw.get("workflow"), 64),
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


def _user_graph(raw: Any) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    gid = _text(raw.get("id"), 64)
    graph = raw.get("graph")
    slots_raw = raw.get("slots")
    if not _ID_RE.fullmatch(gid) or not isinstance(graph, dict) or not isinstance(slots_raw, Mapping):
        return None
    import json

    if len(json.dumps(graph, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_GRAPH_BYTES:
        return None
    slots: dict[str, list[str]] = {}
    for name in ("positive", "negative", "seed", "output"):
        parsed = _slot(slots_raw.get(name))
        if parsed is not None:
            slots[name] = parsed
    if not all(name in slots for name in ("positive", "negative", "seed", "output")):
        return None
    return {
        "id": gid,
        "label": _text(raw.get("label"), 100, gid) or gid,
        "graph": copy.deepcopy(graph),
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
        if item and item["id"] not in graph_seen and item["id"] != "external_core":
            graphs.append(item)
            graph_seen.add(item["id"])
        if len(graphs) >= MAX_USER_GRAPHS:
            break

    workflow = _text(external_raw.get("workflow"), 64, "external_core")
    if workflow != "external_core" and workflow not in graph_seen:
        workflow = "external_core"
    checkpoint = _text(external_raw.get("checkpoint"), 512)
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
        "scene_analysis": bool(raw.get("scene_analysis", False)),
        "timeout_seconds": min(900.0, max(10.0, timeout)),
        "external_comfy": {
            "api_url": url,
            "api_key": _text(external_raw.get("api_key"), 2_048),
            "checkpoint": checkpoint,
            "workflow": workflow,
            "styles": styles,
            "user_graphs": graphs,
        },
    }


def style_defaults_by_id() -> dict[str, dict]:
    return {s["id"]: dict(s) for s in STYLE_DEFAULTS}


def resolve_style(config: Mapping[str, Any], style_id: str) -> dict:
    external = config["external_comfy"]
    style = next((s for s in external["styles"] if s["id"] == style_id), None)
    if style is None:
        raise ValueError(f"unknown image style {style_id!r}")
    defaults = style_defaults_by_id().get(style_id, {})
    return {
        **style,
        "prompt": style["prompt"] or defaults.get("prompt", ""),
        "negative_prompt": style["negative_prompt"] or defaults.get("negative_prompt", ""),
        "checkpoint": style["checkpoint"] or external["checkpoint"],
        "workflow": style["workflow"] or external["workflow"],
    }


def normalize_profile(raw: Mapping[str, Any] | None) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "appearance_prompt": _text(raw.get("appearance_prompt"), 2_000),
        "negative_prompt": _text(raw.get("negative_prompt"), 2_000),
    }
