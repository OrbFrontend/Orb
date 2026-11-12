"""The interactive fragment write rules the fragment routes and the assistant share."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from ...core import (
    DECISION_COLUMNS,
    DECISION_FIELD_TYPE,
    DEFAULT_DECISION_INJECT,
    DEFAULT_STATE_INJECT,
    DEFAULT_STATE_MODE,
    DEFAULT_STATE_UPDATE,
    STATE_COLUMNS,
    STATE_FIELD_TYPE,
)
from ...pipeline.passes.judge import definition_problems

__all__ = ["DecisionProblems", "Stage", "checked_interactive_write", "required_applies", "stage_of"]

# The stages an interactive fragment runs in, as the fragment list groups them; order counts within a stage.
Stage = Literal["judge", "director", "state", "editor"]
_EDITOR_FIELD_TYPES = frozenset(("feedback", "post_processing"))
_STATE_DEFAULTS = {"state_mode": DEFAULT_STATE_MODE, "state_update": DEFAULT_STATE_UPDATE, "state_inject": DEFAULT_STATE_INJECT}


class DecisionProblems(ValueError):
    """A decision definition that would not run, with every problem found."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def stage_of(field_type: str) -> Stage:
    if field_type == DECISION_FIELD_TYPE:
        return "judge"
    if field_type == STATE_FIELD_TYPE:
        return "state"
    return "editor" if field_type in _EDITOR_FIELD_TYPES else "director"


def required_applies(field_type: str, state_mode: str | None, state_update: str | None) -> bool:
    """Whether Required means anything: a field the Director fills, or a single state value it sets before the Writer."""
    if field_type == STATE_FIELD_TYPE:
        return (state_mode or DEFAULT_STATE_MODE) == "value" and state_update == "before_writer"
    return field_type not in ("post_processing", DECISION_FIELD_TYPE)


def _checked_decision_write(payload: dict, existing: Mapping[str, Any] | None) -> dict:
    """Validate the merged decision definition and clear its fields on type changes."""
    merged = {**(existing or {}), **payload}
    if merged.get("field_type") != DECISION_FIELD_TYPE:
        # A partial update that omits field_type leaves the existing type alone.
        if "field_type" in payload:
            payload.update({column: None for column in DECISION_COLUMNS})
        return payload
    problems = definition_problems(merged)
    if problems:
        raise DecisionProblems(problems)
    # Stored explicitly, like a state fragment's settings, so the row says where it goes.
    if not merged.get("decision_inject"):
        payload["decision_inject"] = DEFAULT_DECISION_INJECT
    return payload


def _checked_state_write(payload: dict, existing: Mapping[str, Any] | None) -> dict:
    """Keep state settings on state fragments only, filling defaults for a new one.

    A type change away from state clears them. Changing a state fragment's mode
    rewrites nothing: its saved entries keep their ids and fold the same way.
    """
    field_type = payload.get("field_type", (existing or {}).get("field_type"))
    if field_type != STATE_FIELD_TYPE:
        if "field_type" in payload:
            payload.update({column: None for column in STATE_COLUMNS})
        else:
            for column in STATE_COLUMNS:
                payload.pop(column, None)
        return payload
    for column, default in _STATE_DEFAULTS.items():
        if payload.get(column) is None and not (existing or {}).get(column):
            payload[column] = default
        elif payload.get(column) is None:
            payload.pop(column, None)
    return payload


def _checked_gate_write(payload: dict, existing: Mapping[str, Any] | None) -> dict:
    """Keep a Judge gate on post-processing fragments only.

    A type change away from post-processing clears it, and a partial write to another type discards an incoming one.
    """
    field_type = payload.get("field_type", (existing or {}).get("field_type"))
    if field_type != "post_processing":
        if "field_type" in payload:
            payload["post_processing_gate"] = ""
            payload["post_processing_gate_replies"] = 0
        else:
            payload.pop("post_processing_gate", None)
            payload.pop("post_processing_gate_replies", None)
    return payload


def checked_interactive_write(payload: dict, existing: Mapping[str, Any] | None = None) -> dict:
    """*payload* for a create (no *existing*) or an update of *existing*, its decision, state and gate settings kept to their
    own types and defaulted; :class:`DecisionProblems` when it leaves a decision that would not run."""
    return _checked_gate_write(
        _checked_state_write(_checked_decision_write(payload, existing), existing),
        existing,
    )
