"""Resolve workflow enablement from a settings snapshot."""

from __future__ import annotations

from collections.abc import Mapping

from .registry import list_workflows


def effective_workflow_enabled(workflow_id: str, settings: Mapping) -> bool:
    """True when *workflow_id* is enabled both globally and per-workflow.

    The ``isinstance(dict)`` coercion (rather than ``or {}``) is deliberate: if
    the ``workflow_enabled`` decode in ``get_settings`` ever regresses, the
    column reads back as the raw string ``'{}'`` and ``'{}'.get(...)`` would
    raise on every turn (this runs per subscription per turn). Coercing a stray
    non-dict to ``{}`` degrades to enabled instead of crashing the turn.
    """
    global_on = bool(settings.get("workflows_globally_enabled", 1))
    raw = settings.get("workflow_enabled")
    local_map = raw if isinstance(raw, dict) else {}
    local_on = bool(local_map.get(workflow_id, True))
    return global_on and local_on


def disabled_workflow_tool_names(settings: Mapping) -> set[str]:
    """Tool names owned by workflows that are currently disabled.

    Empty when no disabled workflow declares tools (the case today), so its one
    caller -- the pipeline tool-union strip -- is a no-op then.
    """
    names: set[str] = set()
    for w in list_workflows():
        if not effective_workflow_enabled(w.id, settings):
            names.update(t.name for t in w.tools)
    return names
