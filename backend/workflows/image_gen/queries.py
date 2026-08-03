"""Conversation-less config and capability discovery -- the QUERY hook.

These back the tools-panel card and the settings form. They answer from the saved
config or by probing the backend, with **no conversation in scope**, which is what
separates them from `hooks.py`: nothing here reads history, composes a prompt, or
persists an attachment.

Each reports its own failure in-band as ``{"error": ...}`` rather than raising --
the caller degrades (empty model list, plain-text fields) instead of treating a
probe failure as an HTTP error.
"""

from __future__ import annotations

from ..toolkit import get_workflow_config
from . import pov as pov_mod
from .config import WORKFLOW_ID, active_style, normalize_config
from .engine import ImageGenerationError, comfy_adapter, get_adapter, list_sources
from .engine.providers import provider_catalogue

MAX_INSPECTED_CLASS_TYPES = 200


async def _config_from_query(body) -> dict:
    """The form's unsaved override if the body carries one, else the saved slot.

    The settings form tests and inspects a config it has not saved yet; the
    tools-panel card sends none.
    """
    if isinstance(body, dict) and isinstance(body.get("config"), dict):
        return normalize_config(body["config"])
    return normalize_config(await get_workflow_config(WORKFLOW_ID))


def _default_adapter(config):
    """The adapter for the style that would render next.

    Every action here answers about the default style, deliberately: this backs the
    tools-panel card, whose question is "can the next Visualize render". The settings
    form probes some *other* connection by pointing the default style at it in the
    config it sends (`configForConnection`), so that needs no special case either.
    """
    return get_adapter(config, active_style(config))


async def _status(body) -> dict:
    config = await _config_from_query(body)
    external = config["external_comfy"]
    adapter = _default_adapter(config)
    return {
        "source": config["source"],
        "capabilities": dict(adapter.capabilities),
        "sources": list_sources(),
        "providers": provider_catalogue(),
        "api_url": external["api_url"],
        "default_style": config["default_style"],
        "classifier_ready": await pov_mod.classifier_ready(),
        "fallback_mode": pov_mod.DEFAULT_POV_MODE,
        "style_count": len(config["styles"]),
        "user_graph_count": len(external["user_graphs"]),
        **adapter.readiness(),
        "managed_local": {
            "available": False,
            "reason": "Managed local image generation is not included in this stage",
        },
    }


async def _styles(body) -> dict:
    config = await _config_from_query(body)
    return {
        "source": config["source"],
        "default_style": config["default_style"],
        "styles": config["styles"],
    }


async def _test_connection(body) -> dict:
    explicit = isinstance(body, dict) and isinstance(body.get("config"), dict)
    config = await _config_from_query(body)
    try:
        return await _default_adapter(config).validate_connection(allow_cached=not explicit)
    except (ImageGenerationError, ValueError) as exc:
        return {"error": str(exc)}


async def _external_models(body) -> dict:
    config = await _config_from_query(body)
    try:
        return {"models": await _default_adapter(config).list_models()}
    except ImageGenerationError as exc:
        return {"error": str(exc)}


async def _node_types(body) -> dict:
    """Slot-role typing for the node classes in a graph the user is importing.

    Takes class-type names, not the graph: the browser already parsed it. Dispatches
    to the ComfyUI adapter **explicitly, never by active source** -- imported graphs
    are global and the importer stays usable under cloud. A connection failure
    degrades to no typing; the picker falls back to conventional input names.
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


_QUERY_ACTIONS = {
    "status": _status,
    "styles": _styles,
    "test": _test_connection,
    "models": _external_models,
    "node_types": _node_types,
}


async def query(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    handler = _QUERY_ACTIONS.get(action) if isinstance(action, str) else None
    return await handler(body) if handler else {"error": f"unknown action: {action!r}"}
