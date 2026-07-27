"""Unit tests for the pure workflow-enablement predicates.

``effective_workflow_enabled`` is the ``global AND local`` truth table for a
built-in and the ``local``-only table for a community record (the master is a
built-in-tier switch), with every side defaulting to on when absent, plus a
defensive coercion that turns a stray non-dict ``workflow_enabled`` into
"enabled" rather than raising.
``disabled_workflow_tool_names`` projects the registry to the tool names owned
by currently-disabled workflows; it reads ``list_workflows`` (monkeypatched here
so the test owns the registry view without touching process-global state).
"""

from __future__ import annotations

from backend.workflows import ToolSpec, Workflow, enablement
from backend.workflows.contracts import WorkflowSource
from backend.workflows.enablement import (
    disabled_workflow_tool_names,
    effective_workflow_enabled,
)


def test_truth_table_global_and_local():
    on = {"workflows_globally_enabled": 1, "workflow_enabled": {"w": True}}
    local_off = {"workflows_globally_enabled": 1, "workflow_enabled": {"w": False}}
    global_off = {"workflows_globally_enabled": 0, "workflow_enabled": {"w": True}}
    both_off = {"workflows_globally_enabled": 0, "workflow_enabled": {"w": False}}

    assert effective_workflow_enabled("w", on) is True
    assert effective_workflow_enabled("w", local_off) is False
    assert effective_workflow_enabled("w", global_off) is False
    assert effective_workflow_enabled("w", both_off) is False


def test_community_records_ignore_the_master():
    # The extension tier is decoupled: its own flag is the only gate, so the
    # master going off must leave an installed extension running.
    global_off = {"workflows_globally_enabled": 0, "workflow_enabled": {"ext": True}}
    local_off = {"workflows_globally_enabled": 1, "workflow_enabled": {"ext": False}}

    assert effective_workflow_enabled("ext", global_off, WorkflowSource.COMMUNITY) is True
    assert effective_workflow_enabled("ext", {"workflows_globally_enabled": 0}, WorkflowSource.COMMUNITY) is True
    assert effective_workflow_enabled("ext", local_off, WorkflowSource.COMMUNITY) is False
    # Same settings, built-in tier: the master still applies. The default source
    # is BUILTIN, so a gate that forgets the argument stays under the master.
    assert effective_workflow_enabled("ext", global_off, WorkflowSource.BUILTIN) is False
    assert effective_workflow_enabled("ext", global_off) is False


def test_missing_values_default_to_enabled():
    # Missing local key, missing whole map, missing global column, empty settings:
    # every absence defaults to enabled (the always-on baseline).
    assert effective_workflow_enabled("w", {"workflows_globally_enabled": 1, "workflow_enabled": {}}) is True
    assert effective_workflow_enabled("w", {"workflows_globally_enabled": 1}) is True
    assert effective_workflow_enabled("w", {"workflow_enabled": {"w": True}}) is True
    assert effective_workflow_enabled("w", {}) is True


def test_stray_non_dict_workflow_enabled_degrades_to_enabled():
    # If the decode in get_settings ever regresses, the column reads back as the
    # raw string '{}' (or None). The isinstance coercion must not raise.
    assert effective_workflow_enabled("w", {"workflows_globally_enabled": 1, "workflow_enabled": "{}"}) is True
    assert effective_workflow_enabled("w", {"workflows_globally_enabled": 1, "workflow_enabled": None}) is True


def _probe_tool(name: str) -> ToolSpec:
    return ToolSpec(name=name, schema={"function": {"name": name}}, choice={"type": "function", "function": {"name": name}})


def test_disabled_workflow_tool_names(monkeypatch):
    with_tool = Workflow(id="probe", display_name="Probe", tools=[_probe_tool("probe_tool")])
    no_tool = Workflow(id="plain", display_name="Plain")
    monkeypatch.setattr(enablement, "list_workflows", lambda: [with_tool, no_tool])

    # Probe locally disabled -> its tool name is stripped.
    disabled_local = {"workflows_globally_enabled": 1, "workflow_enabled": {"probe": False}}
    assert disabled_workflow_tool_names(disabled_local) == {"probe_tool"}

    # Everything enabled -> empty set (the no-op case real callers hit today).
    all_on = {"workflows_globally_enabled": 1, "workflow_enabled": {}}
    assert disabled_workflow_tool_names(all_on) == set()

    # Global off disables both; only the tool-bearing one contributes a name.
    global_off = {"workflows_globally_enabled": 0}
    assert disabled_workflow_tool_names(global_off) == {"probe_tool"}
