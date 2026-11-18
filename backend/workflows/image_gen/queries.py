"""Handle conversation-free image workflow capability queries."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from collections.abc import Mapping
from typing import Any

from PIL import Image

from ..toolkit import EVICTED_MARKER, QueryCtx, get_message_by_id, get_workflow_config, gpu_swap_support
from . import pov as pov_mod
from .config import (
    MAX_REFERENCE_SLOTS,
    WORKFLOW_ID,
    active_style,
    normalize_config,
    style_source,
)
from .engine import ImageGenerationError, comfy_adapter, get_adapter, health, list_sources
from .engine.providers import provider_catalogue
from .hooks import review_image_url
from .references import origin_row

MAX_INSPECTED_CLASS_TYPES = 200
# The longest recorded prompt or direction a description quotes.
_DESCRIBED_CHARS = 1500


async def _config_from_query(body) -> dict:
    """The form's unsaved override if the body carries one, else the saved slot.

    The settings form tests and inspects a config it has not saved yet; the tools-panel card sends none.
    """
    if isinstance(body, dict) and isinstance(body.get("config"), dict):
        return normalize_config(body["config"])
    return normalize_config(await get_workflow_config(WORKFLOW_ID))


def _default_adapter(config):
    """The adapter for the style that would render next.

    Every action here answers about the default style, deliberately: this backs the tools-panel card, whose question is "can the
    next Visualize render". The settings form probes some *other* connection by pointing the default style at it in the config
    it sends (`configForConnection`), so that needs no special case either.
    """
    return get_adapter(config, active_style(config))


async def _status(body) -> dict:
    config = await _config_from_query(body)
    external = config["external_comfy"]
    adapter = _default_adapter(config)
    return {
        "source": config["source"],
        "failure": health.verdict(config, active_style(config)),
        "capabilities": dict(adapter.capabilities),
        "sources": list_sources(),
        "providers": provider_catalogue(MAX_REFERENCE_SLOTS),
        "api_url": external["api_url"],
        "default_style": config["default_style"],
        "classifier_ready": await pov_mod.classifier_ready(),
        "fallback_mode": pov_mod.DEFAULT_POV_MODE,
        "style_count": len(config["styles"]),
        "user_graph_count": len(external["user_graphs"]),
        **adapter.readiness(),
        "managed_local": {"available": False, "reason": "Managed local image generation is not included in this stage"},
    }


async def _styles(body) -> dict:
    config = await _config_from_query(body)
    return {
        "source": config["source"],
        "default_style": config["default_style"],
        # Where the next on-demand render runs: the host's GPU swap check asks, since only ComfyUI renders swap.
        "default_source": style_source(config, active_style(config))[0],
        "styles": [{**style, "takes_reference": _takes_reference(config, style)} for style in config["styles"]],
    }


def _takes_reference(config, style) -> bool:
    """Whether this style's backend has an image input a picture could fill, whatever the style's own reference setting.

    The same test a render given a picture applies through `plan_slots`, read off the target without I/O.
    """
    try:
        target = get_adapter(config, {**style, "reference_source": "previous"}).resolve_target(None)
    except (ImageGenerationError, ValueError):
        return False
    return bool(target.reference_slots) or bool(target.reference_capacity and target.reference_template)


def _clipped(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return text if len(text) <= _DESCRIBED_CHARS else f"{text[:_DESCRIBED_CHARS]}…[+{len(text) - _DESCRIBED_CHARS} chars]"


def _pixel_size(data: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            return probe.size
    except Exception:
        return None


def _render_facts(row: Mapping[str, Any]) -> dict:
    """What one of this workflow's own renders recorded about how it was made."""
    try:
        recorded = json.loads(row.get("generation_metadata") or "{}")
    except ValueError:
        return {}
    if not isinstance(recorded, dict):
        return {}
    facts: dict[str, Any] = {key: _clipped(recorded.get(key)) for key in ("style_id", "prompt", "direction")}
    if recorded.get("reference"):
        facts["chosen_reference"] = True
    return {key: value for key, value in facts.items() if value}


async def _describe(body) -> dict:
    """Facts about one chat image by origin, and its preview for a model to look at."""
    origin = body.get("origin") if isinstance(body, dict) else None
    if not isinstance(origin, str) or origin.partition(":")[0] not in ("attachment", "upload"):
        return {"error": "origin names a chat image as attachment:<id> or an upload as upload:<message id>:<id>"}
    try:
        row, message_id = await origin_row(origin)
    except ImageGenerationError as exc:
        return {"error": str(exc)}
    message = await get_message_by_id(message_id)
    upload = origin.startswith("upload:")
    facts: dict[str, Any] = {
        "conversation_id": (message or {}).get("conversation_id"),
        "message_id": message_id,
        "kind": "upload" if upload else "render",
        "mime": row.get("mime_type"),
    }
    if not upload:
        facts["workflow"] = row.get("workflow_id")
        if row.get("workflow_id") == WORKFLOW_ID:
            facts.update(_render_facts(row))
    payload = row.get("data_b64")
    if payload == EVICTED_MARKER:
        facts["evicted"] = True
        return {"facts": facts, "preview_url": ""}
    try:
        data = base64.b64decode(payload or "", validate=True)
    except (ValueError, TypeError):
        data = b""
    size = await asyncio.to_thread(_pixel_size, data) if data else None
    if size is None:
        facts["unreadable"] = True
        return {"facts": facts, "preview_url": ""}
    facts["width"], facts["height"] = size
    return {"facts": facts, "preview_url": await review_image_url(data, str(row.get("mime_type") or ""))}


def _style_by_id(config, style_id: str):
    return next((s for s in config["styles"] if s["id"] == style_id), None)


async def _test_connection(body) -> dict:
    explicit = isinstance(body, dict) and isinstance(body.get("config"), dict)
    config = await _config_from_query(body)
    try:
        return await _default_adapter(config).validate_connection(allow_cached=not explicit)
    except (ImageGenerationError, ValueError) as exc:
        return {"error": str(exc)}


async def _probe(body) -> dict:
    """Probe the selected ComfyUI style without raising to the API caller."""
    config = await _config_from_query(body)
    requested = body.get("style_id") if isinstance(body, dict) else None
    style = _style_by_id(config, requested) if isinstance(requested, str) and requested else active_style(config)
    if style is None:
        return {"failure": None}
    source, _ = style_source(config, style)
    if source != "external_comfy":
        return {"failure": None}
    adapter = comfy_adapter(config, style)
    if not adapter.readiness()["ready"]:
        health.record_success(config, style)
        return {"failure": None}
    try:
        await adapter.validate_bound_style(allow_cached=True)
    except (ImageGenerationError, ValueError) as exc:
        health.record_failure(config, style, str(exc))
    else:
        health.record_success(config, style)
    return {"failure": health.verdict(config, style)}


async def _external_models(body) -> dict:
    config = await _config_from_query(body)
    result: dict = {}
    try:
        result["models"] = await _default_adapter(config).list_models()
    except ImageGenerationError as exc:
        result["error"] = str(exc)
    if style_source(config, active_style(config))[0] == "external_comfy":
        result["graph_models"] = await comfy_adapter(config).graph_models()
    return result


async def _node_types(body) -> dict:
    """Slot-role typing for the node classes in a graph the user is importing.

    Takes class-type names, not the graph: the browser already parsed it. Dispatches to the ComfyUI adapter **explicitly, never
    by active source** -- imported graphs are global and the importer stays usable under cloud. A connection failure degrades to
    no typing; the picker falls back to conventional input names.
    """
    raw = body.get("class_types") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return {"error": "class_types (list of strings) required"}
    class_types = [item for item in raw if isinstance(item, str) and item][:MAX_INSPECTED_CLASS_TYPES]
    config = await _config_from_query(body)
    try:
        return {"nodes": await comfy_adapter(config).node_roles(class_types)}
    except ImageGenerationError:
        return {"nodes": {}}


async def _gpu_swap(ctx: QueryCtx, body) -> dict:
    """Whether the GPU swap option can act: an unloadable LLM lane and a style that renders on ComfyUI."""
    config = await _config_from_query(body)
    support = await gpu_swap_support(ctx.settings)
    comfy_style = any(style_source(config, style)[0] == "external_comfy" for style in config["styles"])
    reason = support["reason"] or ("" if comfy_style else "No style renders on ComfyUI.")
    return {**support, "available": support["available"] and comfy_style, "reason": reason}


_QUERY_ACTIONS = {
    "status": _status,
    "styles": _styles,
    "probe": _probe,
    "test": _test_connection,
    "models": _external_models,
    "node_types": _node_types,
    "describe": _describe,
}


async def query(ctx: QueryCtx, body: dict) -> dict:
    action = body.get("action") if isinstance(body, dict) else None
    if action == "gpu_swap":
        return await _gpu_swap(ctx, body)
    handler = _QUERY_ACTIONS.get(action) if isinstance(action, str) else None
    return await handler(body) if handler else {"error": f"unknown action: {action!r}"}
