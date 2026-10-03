"""Shared instruction fragments for tool-calling pipeline passes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

REASONING_GUIDANCE = " Avoid overthinking."


def field_hint(field_type: str | None) -> str:
    """How a field's value is shaped, as a request states it.

    Keyed on a fragment's ``field_type``. A fixed property passes its JSON Schema
    ``type`` instead, which shares the ``array`` spelling.
    """
    if field_type == "array":
        return "list of strings"
    if field_type == "state":
        return "single value, kept across turns"
    return "single value"


def tool_call_instruction(
    tool_name: str,
    schema: dict,
    *,
    labels: Mapping[str, str] | None = None,
    fragments: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render the ordered single-tool instruction from its live schema.

    State live requiredness here because cached fragment schemas omit it.
    Optional fragments adds per-parameter type hints and row descriptions;
    mood/speaking-plan instructions come from their own sections.
    """
    description = schema["function"]["description"]
    parameters = schema["function"]["parameters"].get("properties", {})

    def _name(key: str) -> str:
        return f'{key} "{labels[key]}"' if labels and labels.get(key) else key

    if fragments is not None and parameters:
        lines = []
        for key, prop in parameters.items():
            fragment = fragments.get(key)
            hint = field_hint(fragment.get("field_type") if fragment else prop.get("type"))
            text = str((fragment or {}).get("description") or "").strip()
            lines.append(f"* {_name(key)} ({hint})" + (f": {text}" if text else ""))
        parameter_block = "Parameters, in order:\n" + "\n".join(lines)
    else:
        parameter_order = ", ".join(_name(key) for key in parameters) if parameters else "N/A"
        parameter_block = f"Parameter order: ({parameter_order})"
    required = schema["function"]["parameters"].get("required") or []
    return (
        f"Call ONLY this tool, ensuring parameters follow the schema order: {tool_name} - {description}\n{parameter_block}"
    ) + (f"\nRequired: {', '.join(required)}" if required else "")
