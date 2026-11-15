"""The stage an interactive fragment runs in; order counts within a stage."""

from __future__ import annotations

from typing import Literal

from .decisions import DECISION_FIELD_TYPE
from .fragment_state import STATE_FIELD_TYPE

FragmentStage = Literal["judge", "director", "state", "editor"]
FRAGMENT_STAGES: tuple[FragmentStage, ...] = ("judge", "director", "state", "editor")
_EDITOR_FIELD_TYPES = frozenset(("feedback", "post_processing"))


def fragment_stage(field_type: str) -> FragmentStage:
    if field_type == DECISION_FIELD_TYPE:
        return "judge"
    if field_type == STATE_FIELD_TYPE:
        return "state"
    return "editor" if field_type in _EDITOR_FIELD_TYPES else "director"
