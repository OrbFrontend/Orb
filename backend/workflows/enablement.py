"""Pure predicates for whether a workflow is currently enabled.

The ``settings`` row is the single source of truth: ``workflows_globally_enabled``
(a master switch over the *built-in* tier only) and ``workflow_enabled`` (a
per-workflow ``{id: bool}`` map, keyed by workflow id and by extension id alike).
A missing global or local value defaults to enabled, so a fresh install and any
future workflow start on. The registry's ``Workflow`` record carries no enabled
flag -- it is rebuilt at import and would lose the state on restart.

Community records are deliberately *not* under the master. An extension is its
own thing: installed, listed, and toggled in the Extensions sidebar, with no
control in the Secondary panel. Folding it under a switch labelled "Secondary
Workflows" made a user who turned off turn-time hooks (TTS, image gen) silently
lose their extension panels and buttons, with the only explanation living in a
different panel. Each tier now answers for itself.

These take an already-loaded settings snapshot rather than reading the DB: every
gate site already holds one, so the predicate stays pure and table-testable with
no in-memory cache mirror to invalidate. The settings-column names live here, not
in ``registry.py``, so the pure registry resolvers stay decoupled from the
settings-row shape.
"""

from __future__ import annotations

from collections.abc import Mapping

from .contracts import WorkflowSource
from .registry import list_workflows


def effective_workflow_enabled(workflow_id: str, settings: Mapping, source: WorkflowSource = WorkflowSource.BUILTIN) -> bool:
    """True when *workflow_id* is enabled: per-record always, master for built-ins.

    Pass the record's own ``source`` (``Workflow.source``, or ``Subscription.source``
    at a hook site -- it mirrors its owner's tier for exactly this kind of check).
    The default keeps every built-in call site a two-argument call; a community
    gate that omits it would silently put the extension back under the master.

    The ``isinstance(dict)`` coercion (rather than ``or {}``) is deliberate: if
    the ``workflow_enabled`` decode in ``get_settings`` ever regresses, the
    column reads back as the raw string ``'{}'`` and ``'{}'.get(...)`` would
    raise on every turn (this runs per subscription per turn). Coercing a stray
    non-dict to ``{}`` degrades to enabled instead of crashing the turn.
    """
    raw = settings.get("workflow_enabled")
    local_map = raw if isinstance(raw, dict) else {}
    if not bool(local_map.get(workflow_id, True)):
        return False
    if source is WorkflowSource.COMMUNITY:
        return True
    return bool(settings.get("workflows_globally_enabled", 1))


def disabled_workflow_tool_names(settings: Mapping) -> set[str]:
    """Tool names owned by workflows that are currently disabled.

    Empty when no disabled workflow declares tools (the case today), so its one
    caller -- the pipeline tool-union strip -- is a no-op then.
    """
    names: set[str] = set()
    for w in list_workflows():
        if not effective_workflow_enabled(w.id, settings, w.source):
            names.update(t.name for t in w.tools)
    return names
