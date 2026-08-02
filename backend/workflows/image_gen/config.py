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
SOURCES = ("external_comfy", "cloud")
DEFAULT_SOURCE = "external_comfy"
MAX_STYLES = 32
MAX_USER_GRAPHS = 32
# How many provider credential sets the config keeps at once. Switching provider
# must not destroy the previous key, so the map is retained rather than replaced --
# and, like everything else here, bounded.
MAX_CLOUD_PROVIDERS = 16
CLOUD_QUALITIES = ("low", "medium", "high")
# Canonical pixel bounds. Dimensions are stored as width/height even for providers
# that speak aspect ratios: one representation, converted at the wire.
MIN_CLOUD_EDGE = 64
MAX_CLOUD_EDGE = 4096
DEFAULT_CLOUD_EDGE = 1024
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
    "source": DEFAULT_SOURCE,
    "default_style": "realistic",
    # Top level, shared by every source. A style is a way of writing the prompt, and
    # that survives a backend switch; `checkpoint`/`workflow` are the ComfyUI-only
    # fields riding along on it.
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
    "pov_mode": DEFAULT_POV_MODE,
    "scene_analysis": False,
    "prompter_reasoning": False,
    "timeout_seconds": 180.0,
    "external_comfy": {
        "api_url": "http://127.0.0.1:8188",
        "api_key": "",
        # A ComfyUI graph is meaningless to any other backend, so this one stays put.
        "user_graphs": [],
    },
    "cloud": {
        "provider": "xai",
        "width": DEFAULT_CLOUD_EDGE,
        "height": DEFAULT_CLOUD_EDGE,
        "quality": "",
        # "" is off. Sending conversation images to a third party is opt-in.
        "reference_source": "",
        # Keyed by provider id so switching provider does not destroy the previous
        # key. One representative entry ships in the defaults so the preset-schema
        # coverage walker can see the `api_key` leaf underneath the map level.
        "providers": {"xai": {"api_key": "", "model": "", "base_url": ""}},
    },
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _text(value: Any, limit: int, default: str = "") -> str:
    return value.strip()[:limit] if isinstance(value, str) else default


def _style(raw: Any) -> dict | None:
    """One style entry, on the now-global style list.

    `checkpoint` and `workflow` are **ComfyUI-only fields on a shared object**: they
    are meaningless under any other source, and are kept per-style rather than moved
    into `external_comfy` so a backend switch and a switch back leave a style's graph
    pin exactly where it was.

    There is deliberately no per-style cloud model. SillyTavern has one model per
    source, and `checkpoint` is a ComfyUI concept; the extension point, should a
    per-style cloud model ever be wanted, is a `style.cloud_model` field read by
    `OpenAICompatibleImageAdapter.resolve_target` in preference to the source-level
    one.
    """
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


def _is_loopback(hostname: str) -> bool:
    host = hostname.lower()
    return host in ("localhost", "::1", "0:0:0:0:0:0:0:1") or host == "127.0.0.1" or host.startswith("127.")


def _cloud_base_url(value: Any) -> str:
    """A user-supplied cloud endpoint override, or "" to use the preset's own.

    The same `urlsplit` rules the ComfyUI URL already gets, plus one more: a cloud
    request carries a bearer key on every call, so plaintext http is only tolerable
    when the "network" is this machine (a local proxy in front of the provider).
    """
    url = _text(value, 2_048)
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if parsed.scheme != "https" and not _is_loopback(parsed.hostname):
        return ""
    return url.rstrip("/")


def _cloud_provider_entry(raw: Any) -> dict:
    raw = raw if isinstance(raw, Mapping) else {}
    return {
        "api_key": _text(raw.get("api_key"), 2_048),
        "model": _text(raw.get("model"), 256),
        "base_url": _cloud_base_url(raw.get("base_url")),
    }


def _edge(value: Any, default: int) -> int:
    try:
        pixels = int(float(value))
    except (TypeError, ValueError):
        return default
    return min(MAX_CLOUD_EDGE, max(MIN_CLOUD_EDGE, pixels))


def _cloud(raw: Any) -> dict:
    """The cloud block, with every provider's credentials kept across a switch.

    An entry whose provider id the preset table does not know is **retained**, not
    dropped: this normalizer runs on GET, the panel assigns that answer into its
    shared config, and `readConfig()` spreads it straight back into the next PUT --
    so dropping a row renamed in a later release would erase the stored key on the
    user's next save, with no error and nothing to recover from. A retained unknown
    id is inert anyway: no preset means no client to build, which is exactly the
    `unknown_provider` readiness reason.
    """
    raw = raw if isinstance(raw, Mapping) else {}
    defaults = CONFIG_DEFAULTS["cloud"]
    provider = _text(raw.get("provider"), 64, defaults["provider"])
    if not _ID_RE.fullmatch(provider):
        provider = defaults["provider"]
    quality = _text(raw.get("quality"), 16).lower()
    if quality not in CLOUD_QUALITIES:
        quality = ""
    reference_source = _text(raw.get("reference_source"), 32)
    if reference_source not in REFERENCE_SOURCES:
        reference_source = ""

    raw_map = raw.get("providers")
    raw_map = raw_map if isinstance(raw_map, Mapping) else {}
    providers: dict[str, dict] = {}
    # The selected provider goes in first, so the cap can never be the reason the
    # credentials the user is actively using are the ones that go missing.
    ordered = [(provider, raw_map.get(provider))] + [(key, value) for key, value in raw_map.items() if key != provider]
    for candidate, entry in ordered:
        pid = _text(candidate, 64)
        if not _ID_RE.fullmatch(pid) or pid in providers:
            continue
        providers[pid] = _cloud_provider_entry(entry)
        if len(providers) >= MAX_CLOUD_PROVIDERS:
            break

    return {
        "provider": provider,
        "width": _edge(raw.get("width"), defaults["width"]),
        "height": _edge(raw.get("height"), defaults["height"]),
        "quality": quality,
        "reference_source": reference_source,
        "providers": providers,
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
    # Styles were nested inside `external_comfy` before they were shared. The
    # normalizer runs on every read and on every write, so a legacy config hoists on
    # first read and persists hoisted on first write -- no DB migration.
    raw_styles = raw.get("styles")
    if not isinstance(raw_styles, list):
        raw_styles = external_raw.get("styles")
    if not isinstance(raw_styles, list):
        raw_styles = CONFIG_DEFAULTS["styles"]
    for candidate in raw_styles:
        item = _style(candidate)
        if item and item["id"] not in seen:
            styles.append(item)
            seen.add(item["id"])
        if len(styles) >= MAX_STYLES:
            break
    if not styles:
        styles = copy.deepcopy(CONFIG_DEFAULTS["styles"])

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

    source = _text(raw.get("source"), 32, DEFAULT_SOURCE)
    if source not in SOURCES:
        source = DEFAULT_SOURCE

    return {
        "source": source,
        "default_style": default_style,
        "styles": styles,
        "pov_mode": normalize_pov_mode(raw.get("pov_mode")),
        "scene_analysis": bool(raw.get("scene_analysis", False)),
        "prompter_reasoning": raw.get("prompter_reasoning") is True,
        "timeout_seconds": min(900.0, max(10.0, timeout)),
        # Never carries `styles` again: the hoist is the migration.
        "external_comfy": {
            "api_url": url,
            "api_key": _text(external_raw.get("api_key"), 2_048),
            "user_graphs": graphs,
        },
        "cloud": _cloud(raw.get("cloud")),
    }


def resolve_style(config: Mapping[str, Any], style_id: str) -> dict:
    style = next((s for s in config["styles"] if s["id"] == style_id), None)
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
