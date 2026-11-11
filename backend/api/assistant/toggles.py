"""The assistant's pipeline switches: the table of stored switches, the tool that sets one, and the summary that reports
them all."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, create_model

from ...core.settings import Settings
from ...database import get_settings, set_enabled_tool, update_settings
from ...features.assistant import AssistantTool, ToolArguments, ToolOutput, ToolSet
from ...pipeline import agent_enabled
from ...workflows import gpu_swap_host, list_workflows, set_workflow_toggle


def _gated(stored: object, master: bool) -> dict[str, bool] | bool:
    """A stored switch, or both values when it is stored on but a master switch above it is off."""
    return bool(stored) if master or not stored else {"stored": True, "effective": False}


def switch_summary(settings: Settings) -> dict[str, Any]:
    """Pipeline switches under the names set_pipeline_toggle takes, with the effective value beside any a master overrides."""
    table = _switch_table()
    agent_on = agent_enabled(settings)
    named = {switch.item for switch in table.values() if switch.key == "enabled_tools"}
    return {
        **{
            name: _gated(switch.read(settings), switch.gate is None or _effective(switch.gate, table, settings))
            for name, switch in table.items()
        },
        "length_guard_limits": {
            "enforce": bool(settings.get("length_guard_enforce")),
            "max_words": settings.get("length_guard_max_words"),
            "max_paragraphs": settings.get("length_guard_max_paragraphs"),
        },
        "prevent_prompt_overrides": bool(settings.get("prevent_prompt_overrides")),
        # The audit checks run only inside the Output Auditor, which runs only with the Agent on.
        "editor_audit_checks": {
            name: _gated(on, _effective("output_auditor", table, settings))
            for name, on in (settings.get("editor_audit_toggles") or {}).items()
        },
        "agent_tools": {
            name: _gated(on, agent_on) for name, on in (settings.get("enabled_tools") or {}).items() if name not in named
        },
    }


@dataclass(frozen=True)
class _Switch:
    """One stored pipeline switch: its label, the settings key it lives in (and its flag's key in a map there), its master."""

    label: str
    key: str
    gate: str | None = None
    item: str = ""
    default: bool = False

    def read(self, settings: Settings) -> bool:
        stored = settings.get(self.key, int(self.default))
        if self.item:
            stored = (stored if isinstance(stored, dict) else {}).get(self.item, self.default)
        return bool(stored)

    async def write(self, on: bool) -> None:
        if self.key == "enabled_tools":
            # One key of the map, so a concurrent change to another tool's flag is kept.
            await set_enabled_tool(self.item, on)
        elif self.key == "workflow_enabled":
            # The workflow route's own path, runtime callbacks included.
            await set_workflow_toggle(self.item, on)
        else:
            # The settings route's own writer: one column, so a concurrent change to any other key is untouched.
            await update_settings({self.key: int(on)})
            if self.key == "workflows_globally_enabled":
                # The GPU swap plan is off while workflows are, so a render right after switching them on must not read a
                # cached "off".
                gpu_swap_host.invalidate()


def _switch_table() -> dict[str, _Switch]:
    """Every toggle name with one stored switch behind it, in a stable order; workflow names follow the registry."""
    table = {
        "agent": _Switch("Agent", "enable_agent", default=True),
        "director": _Switch("Director", "enabled_tools", "agent", item="direct_scene"),
        "director_individual_fragments": _Switch("Individual fragment processing", "director_individual_fragments", "agent"),
        "output_auditor": _Switch("Output Auditor", "enabled_tools", "agent", item="editor_apply_patch"),
        "length_guard": _Switch("Length Guard", "length_guard_enabled", "agent"),
        "agentic_lorebook": _Switch("Agentic Lorebook", "agentic_lorebook_enabled", "agent"),
        "workflows": _Switch("Secondary workflows", "workflows_globally_enabled", default=True),
    }
    for workflow in list_workflows():
        name = f"{workflow.display_name} workflow"
        table[f"workflow:{workflow.id}"] = _Switch(name, "workflow_enabled", "workflows", item=workflow.id, default=True)
    return table


# "Editor" names the Editor pass, which the Output Auditor and the Length Guard each start. Turning it off turns both off;
# turning it on turns on the Output Auditor alone, since the Length Guard is an optional limit the user sets separately.
_EDITOR_OFF = ("output_auditor", "length_guard")

_EDITOR_ON = ("output_auditor",)


def _toggle_switches(toggle: str, on: bool) -> tuple[str, ...]:
    return (_EDITOR_ON if on else _EDITOR_OFF) if toggle == "editor" else (toggle,)


def _effective(name: str, table: Mapping[str, _Switch], settings: Settings) -> bool:
    switch = table[name]
    return switch.read(settings) and (switch.gate is None or _effective(switch.gate, table, settings))


def _toggle_description(table: Mapping[str, _Switch]) -> str:
    return (
        "Turn one pipeline switch on or off. "
        f"Switches: editor, {', '.join(table)}. "
        "editor is the Editor pass: off turns off output_auditor and length_guard, on turns on output_auditor only. "
        "agent is the master of the Director, Output Auditor, Length Guard, Agentic Lorebook, state updates, feedback and "
        "post-processing; workflows (secondary workflows) is the master of each workflow:<id>. A master that is off keeps "
        "the switches under it from taking effect; it is reported, not changed."
    )


def build_toggle_tools() -> list[AssistantTool]:
    """The one tool that sets a pipeline switch."""
    tools = ToolSet()

    table = _switch_table()
    toggle_names = ("editor", *table)
    _ToggleArgs = create_model(
        "_ToggleArgs",
        __base__=ToolArguments,
        toggle=(Literal[toggle_names], ...),
        enabled=(bool, Field(description="true to turn it on, false to turn it off.")),
    )

    def state(name: str, settings: Settings) -> dict[str, bool]:
        return {"stored": table[name].read(settings), "effective": _effective(name, table, settings)}

    @tools.add(
        "write",
        _ToggleArgs,
        _toggle_description(table),
        idempotent=True,
    )
    async def set_pipeline_toggle(args: Any) -> ToolOutput:
        editor, on = args.toggle == "editor", args.enabled
        names = _toggle_switches(args.toggle, on)
        before = await get_settings()
        changed = [name for name in names if table[name].read(before) != on]
        for name in changed:
            await table[name].write(on)
        after = await get_settings()
        # The Editor reports both of its switches whichever way it was set, so turning it on cannot hide the Length Guard.
        switches = {name: state(name, after) for name in (_EDITOR_OFF if editor else names)}
        gates = {gate: state(gate, after) for gate in dict.fromkeys(table[name].gate for name in names) if gate is not None}
        effective = any(switches[name]["effective"] for name in names)
        blocked = [table[gate].label for gate, value in gates.items() if on and not value["effective"]]
        label, wanted = "Editor" if editor else table[args.toggle].label, "on" if on else "off"
        summary = f"Turned {label} {wanted}." if changed else f"{label} was already {wanted}."
        if editor and on:
            # On sets the Output Auditor alone; the Length Guard is a limit the user sets separately.
            guard = "on" if switches["length_guard"]["stored"] else "off; it is set separately"
            summary += f" The Output Auditor is on; the Length Guard stays {guard}."
        if blocked and not effective:
            summary += f" It has no effect while {', '.join(blocked)} is off."
        data: dict[str, Any] = {
            "toggle": args.toggle,
            "requested": on,
            "effective": effective,
            "changed": changed,
            "switches": switches,
            "gates": gates,
        }
        if editor:
            data["note"] = "Post-processing and feedback fragments can still run an Editor pass; they are set per fragment."
        if blocked:
            data["note_gates"] = "A master switch is off. Tell the user; turn it on only if they ask."
        keys = list(dict.fromkeys(table[name].key for name in changed))
        committed = ({"kind": "settings", "keys": keys},) if keys else ()
        return ToolOutput(data, summary, {"toggle": args.toggle, "keys": keys}, committed=committed)

    return tools
