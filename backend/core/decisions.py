"""The decision-fragment authoring contract.

A **decision** is an interactive fragment that asks one question about the
scene, resolves the answer, and supplies its own authored guidance. This module
owns the shape of that definition and nothing else: no rendering, no provider
call, no persistence. It lives in ``core`` because three layers have to agree on
what a valid definition is and none of them may import the others --
``database`` decodes untrusted card fragments, ``api`` validates authoring
writes, and ``pipeline`` decides what is eligible to run.

The first release supports exactly one variant: a yes/no ``noul`` question
placed before the Director. An unknown variant is *rejected*, never coerced into
an ordinary string field -- a card that names a decision type Orb does not
implement must contribute nothing rather than a fragment that silently means
something else.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

DecisionType: TypeAlias = Literal["noul"]
DecisionPlacement: TypeAlias = Literal["before_director"]
DecisionResolution: TypeAlias = Literal["threshold", "roll"]
DecisionOutcome: TypeAlias = Literal["true", "false"]

#: ``interactive_fragments.field_type`` for a decision.
DECISION_FIELD_TYPE = "decision"

#: The supported variants. Each is a closed set in the first release; widening
#: one is a schema and consumer contract change, not a new string.
DECISION_TYPES: frozenset[str] = frozenset({"noul"})
DECISION_PLACEMENTS: frozenset[str] = frozenset({"before_director"})
DECISION_RESOLUTIONS: frozenset[str] = frozenset({"threshold", "roll"})

#: Both outcome keys, in the order they are rendered and serialized. A noul
#: question has exactly these two answers, so criteria and outputs carry exactly
#: these keys -- a missing one is an error, not an implied empty.
OUTCOME_KEYS: tuple[str, ...] = ("true", "false")

#: The authoring columns, in schema order. Spelled once so the schema, the row
#: model, the query write list, the card decoder, and card serialization cannot
#: drift from one another.
DECISION_COLUMNS: tuple[str, ...] = (
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

#: The default template, which needs no summarizer and no authored situation.
#: Its place in the product is a release gate, not an assumption -- see the
#: evidence section of ``docs/plans/decision-fragments.md``.
DEFAULT_STATE_TEMPLATE = "Previous reply:\n{{last_assistant_message}}\n\nCurrent request:\n{{last_message}}"


@dataclass(frozen=True, slots=True)
class DecisionDefinition:
    """One validated decision, ready to render and evaluate.

    Reusable by construction: it holds no random seed, provider credential,
    evaluation result, or local card approval. Those belong to an occurrence,
    to configuration, and to this machine respectively.
    """

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
        """The authored guidance for *outcome*; ``""`` means no injection."""
        return self.outputs.get(outcome, "")


def is_decision_row(row: Mapping[str, Any]) -> bool:
    """Whether *row* claims to be a decision fragment.

    Claims, not is: a row can name the field type and still fail validation.
    Consumers use this to decide whether to *ask* for a definition, then act on
    whether they got one.
    """
    return row.get("field_type") == DECISION_FIELD_TYPE


def _text(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _outcome_map(value: Any, *, allow_empty: bool) -> tuple[dict[str, str], str]:
    """Validate an outcome-keyed JSON object; return ``(map, error)``.

    Accepts an already-decoded mapping or the raw JSON text. Both reach this
    function in practice: the query layer decodes the columns at its read
    boundary, while a preset row or a directly-supplied API payload can still
    carry the string.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except ValueError:
            return {}, "must be a JSON object"
    if not isinstance(value, Mapping):
        return {}, "must be a JSON object"
    if set(value) != set(OUTCOME_KEYS):
        return {}, f"must have exactly the keys {', '.join(OUTCOME_KEYS)}"
    out: dict[str, str] = {}
    for key in OUTCOME_KEYS:
        text = value[key]
        if not isinstance(text, str):
            return {}, f"'{key}' must be a string"
        if not allow_empty and not text.strip():
            return {}, f"'{key}' must not be empty"
        out[key] = text
    return out, ""


def _threshold(row: Mapping[str, Any], resolution: str) -> tuple[float | None, str]:
    """Validate ``decision_threshold`` against the resolution mode.

    Threshold mode requires a finite number in ``[0, 1]``; roll mode requires
    its absence. A stored threshold that roll mode ignores would make two
    definitions with different policies look identical in the Inspector and in
    the resolution-policy fingerprint.
    """
    raw = row.get("decision_threshold")
    if resolution == "roll":
        return (None, "" if raw is None else "decision_threshold must be empty in roll mode")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None, "decision_threshold must be a number between 0 and 1"
    value = float(raw)
    # NaN fails both comparisons, so this also rejects a non-finite threshold.
    if not (0.0 <= value <= 1.0):
        return None, "decision_threshold must be a number between 0 and 1"
    return value, ""


def decision_definition_errors(row: Mapping[str, Any]) -> list[str]:
    """Every reason *row* is not a usable decision, in field order.

    Returns all of them rather than the first: the authoring API reports them
    together, and an author fixing one field at a time through five round trips
    is the same information delivered worse.
    """
    errors: list[str] = []

    decision_type = _text(row, "decision_type")
    if decision_type not in DECISION_TYPES:
        errors.append(f"decision_type must be one of {', '.join(sorted(DECISION_TYPES))}")

    placement = _text(row, "decision_placement")
    if placement not in DECISION_PLACEMENTS:
        errors.append(f"decision_placement must be one of {', '.join(sorted(DECISION_PLACEMENTS))}")

    if not _text(row, "decision_state_template"):
        errors.append("decision_state_template must not be empty")
    if not _text(row, "decision_instructions"):
        errors.append("decision_instructions must not be empty")

    _, criteria_error = _outcome_map(row.get("decision_criteria"), allow_empty=False)
    if criteria_error:
        errors.append(f"decision_criteria {criteria_error}")
    _, outputs_error = _outcome_map(row.get("decision_outputs"), allow_empty=True)
    if outputs_error:
        errors.append(f"decision_outputs {outputs_error}")

    if _text(row, "decision_default") not in OUTCOME_KEYS:
        errors.append(f"decision_default must be one of {', '.join(OUTCOME_KEYS)}")

    resolution = _text(row, "decision_resolution")
    if resolution not in DECISION_RESOLUTIONS:
        errors.append(f"decision_resolution must be one of {', '.join(sorted(DECISION_RESOLUTIONS))}")
    else:
        _, threshold_error = _threshold(row, resolution)
        if threshold_error:
            errors.append(threshold_error)

    return errors


def parse_decision_definition(row: Mapping[str, Any]) -> DecisionDefinition | None:
    """The validated definition in *row*, or ``None`` when it is not usable.

    ``None`` is the whole error contract for the read paths: an untrusted card
    skips the entry, and the pipeline treats a row it cannot parse as missing
    configuration (the author's fallback, with a visible reason). Callers that
    must explain the failure to a person use
    :func:`decision_definition_errors` instead.
    """
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


__all__ = [
    "DECISION_COLUMNS",
    "DECISION_FIELD_TYPE",
    "DECISION_PLACEMENTS",
    "DECISION_RESOLUTIONS",
    "DECISION_TYPES",
    "DEFAULT_STATE_TEMPLATE",
    "OUTCOME_KEYS",
    "DecisionDefinition",
    "DecisionOutcome",
    "DecisionPlacement",
    "DecisionResolution",
    "DecisionType",
    "decision_definition_errors",
    "is_decision_row",
    "parse_decision_definition",
]
