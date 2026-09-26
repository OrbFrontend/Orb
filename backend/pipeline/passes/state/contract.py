"""The turn's captured state-fragment contract and its transport routing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ....core import (
    DECISION_FIELD_TYPE,
    STATE_FIELD_TYPE,
    StateFragment,
    state_fragments_of,
)
from ...predicates import agent_enabled

# Fragment types that are not the Director's per-turn scene values.
NON_SCENE_FIELD_TYPES = ("feedback", STATE_FIELD_TYPE, "post_processing", DECISION_FIELD_TYPE)


@dataclass(frozen=True, slots=True)
class StateContract:
    """State settings captured at turn start for routing, tools, and validation."""

    fragments: tuple[StateFragment, ...] = ()
    updates_on: bool = False

    @classmethod
    def capture(cls, settings: Mapping[str, Any], state_rows: Sequence[Mapping[str, Any]]) -> StateContract:
        return cls(
            fragments=tuple(fragment for fragment in state_fragments_of(state_rows) if fragment.enabled),
            updates_on=agent_enabled(settings),
        )

    def director_values(self) -> tuple[StateFragment, ...]:
        """One-value fragments updated by the Director's ``direct_scene`` call."""
        if not self.updates_on:
            return ()
        return tuple(fragment for fragment in self.fragments if fragment.rides_direct_scene)

    def tool_fragments(self) -> tuple[StateFragment, ...]:
        """Every fragment the ``update_state`` tool carries, before or after the Writer."""
        if not self.updates_on:
            return ()
        return tuple(fragment for fragment in self.fragments if fragment.uses_state_tool)

    def before_writer_tool(self) -> tuple[StateFragment, ...]:
        return tuple(fragment for fragment in self.tool_fragments() if fragment.update == "before_writer")

    def after_reply(self) -> tuple[StateFragment, ...]:
        return tuple(fragment for fragment in self.tool_fragments() if fragment.update == "after_reply")

    def direct_scene_rows(self, fragments: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """Keep scene rows and before-Writer value fragments in their original order."""
        riding = {fragment.id for fragment in self.director_values()}
        return [df for df in fragments if df.get("field_type") not in NON_SCENE_FIELD_TYPES or df.get("id") in riding]

    def to_director(self) -> tuple[StateFragment, ...]:
        return tuple(fragment for fragment in self.fragments if fragment.injects_director)

    def to_writer(self) -> tuple[StateFragment, ...]:
        return tuple(fragment for fragment in self.fragments if fragment.injects_writer)
