from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

DecisionType: TypeAlias = Literal["noul", "choice", "score"]
DecisionPlacement: TypeAlias = Literal["before_director"]
DecisionResolution: TypeAlias = Literal["threshold", "roll", "argmax", "weighted", "nearest"]
DecisionOutcome: TypeAlias = str
DecisionCriteria: TypeAlias = Mapping[str, str] | tuple[str, ...]

DECISION_FIELD_TYPE = "decision"
DECISION_TYPES = frozenset({"noul", "choice", "score"})
DECISION_PLACEMENTS = frozenset({"before_director"})
DECISION_RESOLUTIONS_BY_TYPE: Mapping[str, tuple[str, ...]] = {
    "noul": ("threshold", "roll"),
    "choice": ("argmax", "weighted"),
    "score": ("argmax", "weighted", "nearest"),
}
DECISION_RESOLUTIONS = frozenset(p for policies in DECISION_RESOLUTIONS_BY_TYPE.values() for p in policies)
# The named noul outcome space remains useful for connection-test construction.
# Runtime consumers derive keys from the definition with ``outcome_keys``.
OUTCOME_KEYS = ("true", "false")
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
MAX_DECISION_FACETS = 16
MAX_DECISION_QUESTIONS = 128
MAX_FACET_KEY_LENGTH = 64
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
    "decision_facets",
    "decision_confidence_floor",
)
DEFAULT_STATE_TEMPLATE = "Previous reply:\n{{last_assistant_message}}\n\nCurrent request:\n{{last_message}}"
_FACET_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class DecisionFacet:
    key: str
    label: str
    decision_type: str
    criteria: DecisionCriteria
    instructions: str | Mapping[str, str]
    outputs: Mapping[str, str]
    confidence_floor: float | None = None

    @property
    def outcome_keys(self) -> tuple[str, ...]:
        return criteria_outcome_keys(self.decision_type, self.criteria)

    @property
    def resolution(self) -> str:
        return "threshold" if self.decision_type == "noul" else "argmax"


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
    default_outcome: str
    resolution: str
    threshold: float | None
    facets: tuple[DecisionFacet, ...] = ()
    confidence_floor: float | None = None
    cooldown_turns: int = 0

    @property
    def outcome_keys(self) -> tuple[str, ...]:
        return criteria_outcome_keys(self.decision_type, self.criteria)

    def output_for(self, outcome: str) -> str:
        return self.outputs.get(outcome, "")


def outcome_keys(definition: DecisionDefinition) -> tuple[str, ...]:
    return definition.outcome_keys


def criteria_outcome_keys(decision_type: str, criteria: DecisionCriteria) -> tuple[str, ...]:
    if decision_type == "noul":
        return OUTCOME_KEYS
    if decision_type == "choice" and isinstance(criteria, Mapping):
        return tuple(criteria)
    if decision_type == "score" and not isinstance(criteria, Mapping):
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


def _criteria(value: Any, decision_type: str) -> tuple[DecisionCriteria, str]:
    value = _json(value)
    if decision_type == "score":
        if not isinstance(value, list):
            return (), "must be a JSON array"
        if not MIN_SCORE_LEVELS <= len(value) <= MAX_SCORE_LEVELS:
            return (), f"must contain {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels"
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return (), "levels must be nonempty strings"
        return tuple(value), ""
    if not isinstance(value, Mapping):
        return {}, "must be a JSON object"
    if decision_type == "noul":
        if set(value) != set(OUTCOME_KEYS):
            return {}, f"must have exactly the keys {', '.join(OUTCOME_KEYS)}"
        keys = OUTCOME_KEYS
    elif decision_type == "choice":
        if not 2 <= len(value) <= MAX_CHOICE_OPTIONS:
            return {}, f"must contain 2 to {MAX_CHOICE_OPTIONS} options"
        keys = tuple(value)
        if any(not isinstance(key, str) or not key or len(key) > MAX_FACET_KEY_LENGTH for key in keys):
            return {}, f"option keys must be nonempty strings up to {MAX_FACET_KEY_LENGTH} characters"
    else:
        return {}, "cannot be validated for an unknown decision type"
    for key in keys:
        if not isinstance(value[key], str) or not value[key].strip():
            return {}, f"'{key}' must be a nonempty string"
    return {key: value[key] for key in keys}, ""


def _outputs(value: Any, keys: Sequence[str]) -> tuple[dict[str, str], str]:
    value = _json(value)
    if not isinstance(value, Mapping):
        return {}, "must be a JSON object"
    if set(value) != set(keys):
        return {}, f"must have exactly the keys {', '.join(keys)}"
    for key in keys:
        if not isinstance(value[key], str):
            return {}, f"'{key}' must be a string"
    return {key: value[key] for key in keys}, ""


def _confidence(value: Any, *, decision_type: str, field: str) -> tuple[float | None, str]:
    if value is None:
        return None, ""
    if decision_type == "noul":
        return None, f"{field} must be empty for noul"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"{field} must be a number between 0 and 1"
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        return None, f"{field} must be a number between 0 and 1"
    return number, ""


def _threshold(row: Mapping[str, Any], decision_type: str, resolution: str) -> tuple[float | None, str]:
    raw = row.get("decision_threshold")
    if decision_type != "noul" or resolution == "roll":
        return None, "" if raw is None else "decision_threshold must be empty for this resolution"
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "decision_threshold must be a number between 0 and 1"
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        return None, "decision_threshold must be a number between 0 and 1"
    return value, ""


def _facets(value: Any, primary_keys: Sequence[str]) -> tuple[tuple[DecisionFacet, ...], list[str]]:
    value = _json(value)
    if value is None:
        return (), []
    if not isinstance(value, list):
        return (), ["decision_facets must be a JSON array"]
    if len(value) > MAX_DECISION_FACETS:
        return (), [f"decision_facets must contain at most {MAX_DECISION_FACETS} facets"]
    facets: list[DecisionFacet] = []
    errors: list[str] = []
    seen: set[str] = set()
    questions = 1
    for index, raw in enumerate(value):
        prefix = f"decision_facets[{index}]"
        if not isinstance(raw, Mapping):
            errors.append(f"{prefix} must be an object")
            continue
        if "facets" in raw:
            errors.append(f"{prefix} facets cannot nest")
        key = raw.get("key")
        if not isinstance(key, str) or not _FACET_KEY.fullmatch(key) or len(key) > MAX_FACET_KEY_LENGTH:
            errors.append(
                f"{prefix}.key must use lowercase letters, numbers, '_' or '-' and be at most {MAX_FACET_KEY_LENGTH} characters"
            )
            key = ""
        elif key in seen:
            errors.append(f"{prefix}.key must be unique")
        else:
            seen.add(key)
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{prefix}.label must not be empty")
            label = key
        decision_type = raw.get("type")
        if decision_type not in DECISION_TYPES:
            errors.append(f"{prefix}.type must be one of {', '.join(sorted(DECISION_TYPES))}")
            continue
        criteria, error = _criteria(raw.get("criteria"), decision_type)
        if error:
            errors.append(f"{prefix}.criteria {error}")
            continue
        facet_keys = criteria_outcome_keys(decision_type, criteria)
        outputs, error = _outputs(raw.get("outputs"), facet_keys)
        if error:
            errors.append(f"{prefix}.outputs {error}")
        instructions = raw.get("instructions")
        parsed_instructions: str | Mapping[str, str]
        if isinstance(instructions, str):
            if not instructions.strip():
                errors.append(f"{prefix}.instructions must not be empty")
            parsed_instructions = instructions
            questions += 1
        elif isinstance(instructions, Mapping):
            if set(instructions) != set(primary_keys):
                errors.append(f"{prefix}.instructions must have exactly the keys {', '.join(primary_keys)}")
            if any(
                not isinstance(instructions.get(branch), str) or not str(instructions.get(branch)).strip()
                for branch in primary_keys
            ):
                errors.append(f"{prefix}.instructions branches must be nonempty strings")
            parsed_instructions = {branch: str(instructions.get(branch) or "") for branch in primary_keys}
            questions += len(primary_keys)
        else:
            errors.append(f"{prefix}.instructions must be a string or an object keyed by the primary outcomes")
            parsed_instructions = ""
        confidence, error = _confidence(
            raw.get("confidence_floor"), decision_type=decision_type, field=f"{prefix}.confidence_floor"
        )
        if error:
            errors.append(error)
        facets.append(
            DecisionFacet(
                key=key,
                label=str(label),
                decision_type=decision_type,
                criteria=criteria,
                instructions=parsed_instructions,
                outputs=outputs,
                confidence_floor=confidence,
            )
        )
    if questions > MAX_DECISION_QUESTIONS:
        errors.append(f"decision fan-out must contain at most {MAX_DECISION_QUESTIONS} questions")
    return tuple(facets), errors


def decision_definition_errors(row: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    decision_type = _text(row, "decision_type")
    if decision_type not in DECISION_TYPES:
        errors.append(f"decision_type must be one of {', '.join(sorted(DECISION_TYPES))}")
    if _text(row, "decision_placement") not in DECISION_PLACEMENTS:
        errors.append(f"decision_placement must be one of {', '.join(sorted(DECISION_PLACEMENTS))}")
    for field in ("decision_state_template", "decision_instructions"):
        if not _text(row, field):
            errors.append(f"{field} must not be empty")

    criteria, error = _criteria(row.get("decision_criteria"), decision_type)
    if error:
        errors.append(f"decision_criteria {error}")
    keys = criteria_outcome_keys(decision_type, criteria)
    _, error = _outputs(row.get("decision_outputs"), keys)
    if error:
        errors.append(f"decision_outputs {error}")
    if _text(row, "decision_default") not in keys:
        errors.append(f"decision_default must be one of {', '.join(keys)}")

    resolution = _text(row, "decision_resolution")
    policies = DECISION_RESOLUTIONS_BY_TYPE.get(decision_type, ())
    if resolution not in policies:
        errors.append(f"decision_resolution must be one of {', '.join(policies)}")
    if resolution in policies:
        _, error = _threshold(row, decision_type, resolution)
        if error:
            errors.append(error)
    _, error = _confidence(row.get("decision_confidence_floor"), decision_type=decision_type, field="decision_confidence_floor")
    if error:
        errors.append(error)
    _, facet_errors = _facets(row.get("decision_facets"), keys)
    errors.extend(facet_errors)
    return errors


def parse_decision_definition(row: Mapping[str, Any]) -> DecisionDefinition | None:
    if not is_decision_row(row) or decision_definition_errors(row):
        return None
    decision_type = _text(row, "decision_type")
    resolution = _text(row, "decision_resolution")
    criteria, _ = _criteria(row.get("decision_criteria"), decision_type)
    keys = criteria_outcome_keys(decision_type, criteria)
    outputs, _ = _outputs(row.get("decision_outputs"), keys)
    threshold, _ = _threshold(row, decision_type, resolution)
    confidence, _ = _confidence(
        row.get("decision_confidence_floor"), decision_type=decision_type, field="decision_confidence_floor"
    )
    facets, _ = _facets(row.get("decision_facets"), keys)
    label = str(row.get("label") or "")
    return DecisionDefinition(
        fragment_id=str(row["id"]),
        label=label,
        injection_label=str(row.get("injection_label") or "") or label,
        decision_type=decision_type,
        placement=_text(row, "decision_placement"),
        state_template=str(row["decision_state_template"]),
        instructions=str(row["decision_instructions"]),
        criteria=criteria,
        outputs=outputs,
        default_outcome=_text(row, "decision_default"),
        resolution=resolution,
        threshold=threshold,
        facets=facets,
        confidence_floor=confidence,
        cooldown_turns=int(row.get("cooldown_turns") or 0),
    )
