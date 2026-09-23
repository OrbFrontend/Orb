from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypeAlias

DecisionCriteria: TypeAlias = Mapping[str, str] | tuple[str, ...]

DECISION_FIELD_TYPE = "decision"
DECISION_RESOLUTIONS_BY_TYPE: Mapping[str, tuple[str, ...]] = {
    "noul": ("threshold", "roll"),
    "choice": ("argmax", "weighted"),
    "score": ("argmax", "weighted", "nearest"),
}
DECISION_TYPES = frozenset(DECISION_RESOLUTIONS_BY_TYPE)
DECISION_PLACEMENT = "before_director"
OUTCOME_KEYS = ("true", "false")
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
MAX_OPTION_KEY_LENGTH = 64
DECISION_COLUMNS = (
    "decision_type",
    "decision_placement",
    "decision_state_template",
    "decision_instructions",
    "decision_criteria",
    "decision_outputs",
    "decision_resolution",
    "decision_threshold",
    "decision_confidence_floor",
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
    criteria: DecisionCriteria
    outputs: Mapping[str, str]
    resolution: str
    threshold: float | None
    confidence_floor: float | None = None
    cooldown_turns: int = 0

    @property
    def outcome_keys(self) -> tuple[str, ...]:
        return _outcome_keys(self.decision_type, self.criteria)


def _outcome_keys(decision_type: str, criteria: Any) -> tuple[str, ...]:
    if decision_type == "noul":
        return OUTCOME_KEYS
    if decision_type == "choice" and isinstance(criteria, Mapping):
        return tuple(criteria)
    if decision_type == "score" and isinstance(criteria, tuple):
        return tuple(str(index) for index in range(len(criteria)))
    return ()


def is_decision_row(row: Mapping[str, Any]) -> bool:
    return row.get("field_type") == DECISION_FIELD_TYPE


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value) if value.strip() else None
    except ValueError:
        return None


def _unit(value: Any) -> float | None:
    """*value* as a float in [0, 1], or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 1 else None


def _criteria(value: Any, decision_type: str) -> tuple[DecisionCriteria | None, str]:
    value = _json(value)
    if decision_type == "score":
        if not isinstance(value, list):
            return None, "must be a JSON array"
        if not MIN_SCORE_LEVELS <= len(value) <= MAX_SCORE_LEVELS:
            return None, f"must contain {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels"
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return None, "levels must be nonempty strings"
        return tuple(value), ""
    if not isinstance(value, Mapping):
        return None, "must be a JSON object"
    if decision_type == "noul":
        if set(value) != set(OUTCOME_KEYS):
            return None, f"must have exactly the keys {', '.join(OUTCOME_KEYS)}"
        keys = OUTCOME_KEYS
    elif decision_type == "choice":
        if not 2 <= len(value) <= MAX_CHOICE_OPTIONS:
            return None, f"must contain 2 to {MAX_CHOICE_OPTIONS} options"
        keys = tuple(value)
        if any(not isinstance(key, str) or not key or len(key) > MAX_OPTION_KEY_LENGTH for key in keys):
            return None, f"option keys must be nonempty strings up to {MAX_OPTION_KEY_LENGTH} characters"
    else:
        return None, "cannot be validated for an unknown decision type"
    for key in keys:
        if not isinstance(value[key], str) or not value[key].strip():
            return None, f"'{key}' must be a nonempty string"
    return {key: value[key] for key in keys}, ""


def _outputs(value: Any, keys: tuple[str, ...]) -> tuple[dict[str, str] | None, str]:
    value = _json(value)
    if not isinstance(value, Mapping):
        return None, "must be a JSON object"
    if set(value) != set(keys):
        return None, f"must have exactly the keys {', '.join(keys)}"
    for key in keys:
        if not isinstance(value[key], str):
            return None, f"'{key}' must be a string"
    return {key: value[key] for key in keys}, ""


def _parse(row: Mapping[str, Any]) -> tuple[DecisionDefinition | None, list[str]]:
    """Validate *row* as a whole and build its definition in the same pass."""
    errors: list[str] = []
    decision_type = _text(row, "decision_type")
    if decision_type not in DECISION_TYPES:
        errors.append(f"decision_type must be one of {', '.join(sorted(DECISION_TYPES))}")
    if _text(row, "decision_placement") != DECISION_PLACEMENT:
        errors.append(f"decision_placement must be one of {DECISION_PLACEMENT}")
    for column in ("decision_state_template", "decision_instructions"):
        if not _text(row, column):
            errors.append(f"{column} must not be empty")

    criteria, error = _criteria(row.get("decision_criteria"), decision_type)
    if error:
        errors.append(f"decision_criteria {error}")
    outputs, error = _outputs(row.get("decision_outputs"), _outcome_keys(decision_type, criteria))
    if error:
        errors.append(f"decision_outputs {error}")

    resolution = _text(row, "decision_resolution")
    policies = DECISION_RESOLUTIONS_BY_TYPE.get(decision_type, ())
    raw_threshold = row.get("decision_threshold")
    threshold = None
    if resolution not in policies:
        errors.append(f"decision_resolution must be one of {', '.join(policies)}")
    elif resolution == "threshold":
        if (threshold := _unit(raw_threshold)) is None:
            errors.append("decision_threshold must be a number between 0 and 1")
    elif raw_threshold is not None:
        errors.append("decision_threshold must be empty for this resolution")

    raw_floor = row.get("decision_confidence_floor")
    floor = None
    if raw_floor is not None:
        if decision_type == "noul":
            errors.append("decision_confidence_floor must be empty for noul")
        elif (floor := _unit(raw_floor)) is None:
            errors.append("decision_confidence_floor must be a number between 0 and 1")

    if errors or criteria is None or outputs is None:
        return None, errors
    label = str(row.get("label") or "")
    return DecisionDefinition(
        fragment_id=str(row["id"]),
        label=label,
        injection_label=str(row.get("injection_label") or "") or label,
        decision_type=decision_type,
        placement=DECISION_PLACEMENT,
        state_template=str(row["decision_state_template"]),
        instructions=str(row["decision_instructions"]),
        criteria=criteria,
        outputs=outputs,
        resolution=resolution,
        threshold=threshold,
        confidence_floor=floor,
        cooldown_turns=int(row.get("cooldown_turns") or 0),
    ), []


def decision_definition_errors(row: Mapping[str, Any]) -> list[str]:
    return _parse(row)[1]


def parse_decision_definition(row: Mapping[str, Any]) -> DecisionDefinition | None:
    return _parse(row)[0] if is_decision_row(row) else None
