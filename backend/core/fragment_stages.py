"""The stage an interactive fragment runs in; order counts within a stage, and decisions share the Director's."""

from __future__ import annotations

from typing import Literal

from .decisions import DECISION_FIELD_TYPE
from .fragment_state import STATE_FIELD_TYPE

FragmentStage = Literal["director", "state", "editor"]
FRAGMENT_STAGES: tuple[FragmentStage, ...] = ("director", "state", "editor")
# The Editor applies these to the reply in one ordered, gated step: recast patches it, rewrite replaces it whole.
REPLY_EDIT_FIELD_TYPES = frozenset(("recast", "rewrite"))
_EDITOR_FIELD_TYPES = frozenset(("feedback", *REPLY_EDIT_FIELD_TYPES))


def fragment_stage(field_type: str) -> FragmentStage:
    if field_type == STATE_FIELD_TYPE:
        return "state"
    return "editor" if field_type in _EDITOR_FIELD_TYPES else "director"


def is_director_fragment(field_type: str) -> bool:
    """Whether a fragment of *field_type* is one the Director fills, as opposed to a decision sharing its stage."""
    return field_type != DECISION_FIELD_TYPE and fragment_stage(field_type) == "director"
