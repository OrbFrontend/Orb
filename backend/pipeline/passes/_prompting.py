"""Shared instruction fragments for tool-calling pipeline passes."""

from __future__ import annotations

from collections.abc import Mapping

REASONING_GUIDANCE = " Avoid overthinking."


def tool_call_instruction(
    tool_name: str,
    schema: dict,
    *,
    labels: Mapping[str, str] | None = None,
) -> str:
    """Render the ordered single-tool instruction used by pipeline passes."""
    description = schema["function"]["description"]
    parameters = schema["function"]["parameters"].get("properties", {})
    if not parameters:
        parameter_order = "N/A"
    elif labels:
        parameter_order = ", ".join(f'{key} ("{labels[key]}")' if labels.get(key) else key for key in parameters)
    else:
        parameter_order = ", ".join(parameters.keys())
    return (
        "Call ONLY this tool, ensuring parameters follow the schema order: "
        f"{tool_name} - {description}\nParameter order: ({parameter_order})"
    )
