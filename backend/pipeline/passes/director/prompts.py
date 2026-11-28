"""Instruction prompts owned by the Director passes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ....prompting.tool_catalog import get_tool, require_tool
from .._prompting import field_hint, tool_call_instruction
from ..judge import PlacedQuestion

DIRECTOR_PREAMBLE = (
    "[OOC: Pause to direct the scene. Use tool calls to accomplish your task "
    "accurately and creatively. Your output will directly influence the scenario. "
    "Think outside the box, be intentional."
)


def _moods_options_block(
    active_moods: Sequence[str], mood_fragments: Sequence[Mapping[str, Any]], resting: frozenset[str] = frozenset()
) -> str:
    # A resting mood is neither offered nor carried into this turn, so listing it
    # as previously active only invites the model to pick it again.
    moods = ", ".join(mood for mood in active_moods if mood not in resting) or "none"
    fragments = "\n".join(
        f"* [{fragment['id']}] - {fragment['description']}" for fragment in mood_fragments if fragment["id"] not in resting
    )
    return f"Previously active moods: {moods}\n\nAvailable writing moods:\n{fragments}"


def build_director_tool_prompt(
    tool_name: str,
    user_message: str,
    active_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]] | None = None,
    progressive_state: dict | None = None,
    tool_schema: dict | None = None,
    cast_instruction: str = "",
    resting: frozenset[str] = frozenset(),
    unavailable_fields: Sequence[str] = (),
    elsewhere_fields: Sequence[str] = (),
    decided_fields: Sequence[tuple[str, Any]] = (),
    fills_moods: bool = True,
    questions: Sequence[PlacedQuestion] = (),
) -> str:
    """Build the combined Director request for one tool.

    *tool_schema* is the live view whose parameters the model may fill; *unavailable_fields* are the ones the shared schema
    still offers -- disabled or resting fragments -- which the request names so the model leaves them empty. ``direct_scene``
    lists each live field with its description here, because the shared schema carries fragment fields by name only.

    A turn with a Judge point between Director fragments directs the scene in several requests: *elsewhere_fields* are the
    live fields another of them fills, *decided_fields* what the earlier ones decided, and only the last fills moods.
    *questions* are the visible decisions for this request.
    """
    tool = get_tool(tool_name)
    if not tool:
        return ""
    schema = tool_schema if tool_schema is not None else tool["schema"]
    preamble = DIRECTOR_PREAMBLE
    fragments = (
        {fragment["id"]: fragment for fragment in (interactive_fragments or [])} if tool_name == "direct_scene" else None
    )
    parts = [preamble, tool_call_instruction(tool_name, schema, fragments=fragments)]
    if tool_name == "direct_scene":
        if cast_instruction:
            parts.append(cast_instruction)
        progressive_lines = [
            f"* {fragment['id']}: {(progressive_state or {}).get(fragment['id'])}"
            for fragment in (interactive_fragments or [])
            if fragment.get("field_type") == "state" and (progressive_state or {}).get(fragment["id"])
        ]
        if progressive_lines:
            parts.append(
                "Saved state fields - leave a field empty to keep it; write its complete new value only if it "
                "changed:\n" + "\n".join(progressive_lines)
            )
        if unavailable_fields:
            parts.append(f"Interactive fields unavailable this turn (leave empty): {', '.join(unavailable_fields)}")
        if elsewhere_fields:
            parts.append(f"Interactive fields filled in another request this turn (leave empty): {', '.join(elsewhere_fields)}")
        if decided := _decided_block(decided_fields):
            parts.append(decided)
        if upcoming := question_block(questions):
            parts.append(upcoming)
        if fills_moods:
            parts.append(_moods_options_block(active_moods, mood_fragments, resting))
        else:
            parts.append("Leave moods empty: they are chosen in a later request.")
        parts.append(f'User\'s next message (for context, take this into account when directing):\n"""{user_message}"""')
    return "\n\n".join(parts) + "]"


def _render_decided(value: Any) -> str:
    return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)


def _decided_block(decided_fields: Sequence[tuple[str, Any]]) -> str:
    prior = [f"- {label}: {_render_decided(value)}" for label, value in decided_fields if value]
    return "Decided so far this turn (build on these, do not contradict):\n" + "\n".join(prior) if prior else ""


def question_block(questions: Sequence[PlacedQuestion]) -> str:
    recipients = {
        "writer": "The Writer follows its answer.",
        "director": "Later Director steps follow its answer.",
        "both": "Later Director steps and the Writer follow its answer.",
    }
    lines = [f"- {q.text} {recipients[q.inject]}" for q in questions if q.text]
    return "Leave room for the Judge's answers to these questions:\n" + "\n".join(lines) if lines else ""


def ruling_entry(label: str, guidance: str, *, overrides: bool = True) -> tuple[str, str]:
    """A Judge ruling as a decided-so-far line; with *overrides* (a plan above it) it stands where it differs from that plan."""
    return (
        f"Judge ruling ({label})",
        f"{guidance} Where it differs from the plan above, the ruling stands." if overrides else guidance,
    )


def _value_field_lines(fragment: Mapping[str, Any], prior: Any) -> list[str]:
    """A one-value state field's lines: what it records, then its saved value when it has one."""
    lines = [f"Field '{fragment['id']}' ({field_hint(fragment['field_type'])}): {fragment['description']}"]
    if prior:
        lines.append(
            f"Current value (leave the field empty to keep it; write the complete new value only if it changed): {prior}"
        )
    return lines


def build_director_scene_step_prompt(
    user_message: str,
    active_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    *,
    tool_schema: dict | None = None,
    target_fragment: Mapping[str, Any] | None = None,
    decided_fields: Sequence[tuple[str, Any]] = (),
    cast_instruction: str = "",
    resting: frozenset[str] = frozenset(),
    value_fields: Sequence[Mapping[str, Any]] = (),
    value_priors: Mapping[str, Any] | None = None,
    questions: Sequence[PlacedQuestion] = (),
) -> str:
    """Build one ``direct_scene`` request targeting a single output.

    The last request, with no *target_fragment*, fills the moods and the one-value state *value_fields*, each shown its
    saved value from *value_priors*, so they are set with the whole scene in view. *questions* are the visible decisions for this request.
    """
    schema = tool_schema if tool_schema is not None else require_tool("direct_scene")["schema"]
    description = schema["function"]["description"]
    parts = [DIRECTOR_PREAMBLE]

    if target_fragment is None:
        targets = ", ".join(["moods", *(fragment["id"] for fragment in value_fields)])
        required = [fragment["id"] for fragment in value_fields if fragment.get("required")]
        parts.append(
            f"Call ONLY direct_scene - {description}\nFill ONLY: {targets}."
            + (f"\nRequired: {', '.join(required)}" if required else "")
        )
        scene = [f"- {label}: {_render_decided(value)}" for label, value in decided_fields if value]
        if scene:
            parts.append("Scene direction decided this turn (pick moods that fit it):\n" + "\n".join(scene))
        for fragment in value_fields:
            parts.append("\n".join(_value_field_lines(fragment, (value_priors or {}).get(fragment["id"]))))
        parts.append(_moods_options_block(active_moods, mood_fragments, resting))
    else:
        fragment_id = target_fragment["id"]
        hint = field_hint(target_fragment["field_type"])
        parts.append(
            f"Call ONLY direct_scene - {description}\nFill ONLY the '{fragment_id}' parameter. "
            "Leave moods and all other fields empty."
        )
        parts.append(f"Field '{fragment_id}' ({hint}): {target_fragment['description']}")
        if cast_instruction:
            parts.append(cast_instruction)
        if decided := _decided_block(decided_fields):
            parts.append(decided)
    if upcoming := question_block(questions):
        parts.append(upcoming)

    parts.append(f'User\'s next message (context):\n"""{user_message}"""')
    return "\n\n".join(parts) + "]"


def build_lorebook_select_prompt(catalog: str, user_message: str) -> str:
    """Build the standalone Agentic Lorebook selection request."""
    parts = [
        DIRECTOR_PREAMBLE,
        (
            "Call ONLY select_lorebook. From the catalog below, choose ONLY the entries relevant to the "
            "current scene and the user's next message (quoted after the catalog); leave the selection "
            "empty if none apply. Your selection fully controls activation of these entries; "
            "keywords are relevance hints and do not activate entries automatically."
        ),
        catalog,
        f'User\'s next message:\n"""{user_message}"""',
    ]
    return "\n\n".join(parts) + "]"
