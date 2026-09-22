from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

DecisionType: TypeAlias = Literal["noul"]
DecisionPlacement: TypeAlias = Literal["before_director"]
DecisionResolution: TypeAlias = Literal["threshold", "roll"]
DecisionOutcome: TypeAlias = Literal["true", "false"]

DECISION_FIELD_TYPE = "decision"
DECISION_TYPES = frozenset({"noul"})
DECISION_PLACEMENTS = frozenset({"before_director"})
DECISION_RESOLUTIONS = frozenset({"threshold", "roll"})
OUTCOME_KEYS = ("true", "false")
DECISION_COLUMNS = (
    "decision_type",
    "decision_placement",
    "decision_state_template",
    "decision_instructions",
    "decision_criteria",
    "decision_outputs",
    "decision_default",
    "decision_resolution",
    "decision_threshold",
)
DEFAULT_STATE_TEMPLATE = "Previous reply:\n{{last_assistant_message}}\n\nCurrent request:\n{{last_message}}"


@dataclass(frozen=True, slots=True)
class DecisionDefinition:
    fragment_id: str
    label: str
    injection_label: str
    decision_type: str
    placement: str
    state_template: str
    instructions: str
    criteria: Mapping[str, str]
    outputs: Mapping[str, str]
    default_outcome: str
    resolution: str
    threshold: float | None
    cooldown_turns: int = 0

    def output_for(self, outcome: str) -> str:
        return self.outputs.get(outcome, "")


def is_decision_row(row: Mapping[str, Any]) -> bool:
    return row.get("field_type") == DECISION_FIELD_TYPE


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _outcome_map(value: Any, *, allow_empty: bool) -> tuple[dict[str, str], str]:
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except ValueError:
            return {}, "must be a JSON object"
    if not isinstance(value, Mapping):
        return {}, "must be a JSON object"
    if set(value) != set(OUTCOME_KEYS):
        return {}, f"must have exactly the keys {', '.join(OUTCOME_KEYS)}"
    for key in OUTCOME_KEYS:
        if not isinstance(value[key], str):
            return {}, f"'{key}' must be a string"
        if not allow_empty and not value[key].strip():
            return {}, f"'{key}' must not be empty"
    return {key: value[key] for key in OUTCOME_KEYS}, ""


def _threshold(row: Mapping[str, Any], resolution: str) -> tuple[float | None, str]:
    raw = row.get("decision_threshold")
    if resolution == "roll":
        return None, "" if raw is None else "decision_threshold must be empty in roll mode"
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "decision_threshold must be a number between 0 and 1"
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None, "decision_threshold must be a number between 0 and 1"
    return value, ""


def decision_definition_errors(row: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field, choices in (
        ("decision_type", DECISION_TYPES),
        ("decision_placement", DECISION_PLACEMENTS),
    ):
        if _text(row, field) not in choices:
            errors.append(f"{field} must be one of {', '.join(sorted(choices))}")
    for field in ("decision_state_template", "decision_instructions"):
        if not _text(row, field):
            errors.append(f"{field} must not be empty")
    for field, allow_empty in (("decision_criteria", False), ("decision_outputs", True)):
        _, error = _outcome_map(row.get(field), allow_empty=allow_empty)
        if error:
            errors.append(f"{field} {error}")
    for field, choices in (
        ("decision_default", frozenset(OUTCOME_KEYS)),
        ("decision_resolution", DECISION_RESOLUTIONS),
    ):
        if _text(row, field) not in choices:
            errors.append(f"{field} must be one of {', '.join(sorted(choices))}")
    resolution = _text(row, "decision_resolution")
    if resolution in DECISION_RESOLUTIONS:
        _, error = _threshold(row, resolution)
        if error:
            errors.append(error)
    return errors


def parse_decision_definition(row: Mapping[str, Any]) -> DecisionDefinition | None:
    if not is_decision_row(row) or decision_definition_errors(row):
        return None
    resolution = _text(row, "decision_resolution")
    criteria, _ = _outcome_map(row.get("decision_criteria"), allow_empty=False)
    outputs, _ = _outcome_map(row.get("decision_outputs"), allow_empty=True)
    threshold, _ = _threshold(row, resolution)
    label = str(row.get("label") or "")
    return DecisionDefinition(
        fragment_id=str(row["id"]),
        label=label,
        injection_label=str(row.get("injection_label") or "") or label,
        decision_type=_text(row, "decision_type"),
        placement=_text(row, "decision_placement"),
        state_template=str(row["decision_state_template"]),
        instructions=str(row["decision_instructions"]),
        criteria=criteria,
        outputs=outputs,
        default_outcome=_text(row, "decision_default"),
        resolution=resolution,
        threshold=threshold,
        cooldown_turns=int(row.get("cooldown_turns") or 0),
    )
