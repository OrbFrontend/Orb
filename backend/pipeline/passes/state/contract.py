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
    """Enabled state fragments as configured when the turn started.

    Captured once per turn from the same fragment rows the turn loaded, and used
    for tool construction, routing, validation, and commit alike, so a setting
    edited mid-turn takes effect on the next turn and never reinterprets an
    in-flight result. ``updates_on`` is the Agent toggle and the global State
    updates switch together: off, no automatic update runs, while injection --
    independent by design -- still does.
    """

    fragments: tuple[StateFragment, ...] = ()
    updates_on: bool = False

    @classmethod
    def capture(cls, settings: Mapping[str, Any], state_rows: Sequence[Mapping[str, Any]]) -> StateContract:
        return cls(
            fragments=tuple(fragment for fragment in state_fragments_of(state_rows) if fragment.enabled),
            updates_on=agent_enabled(settings) and bool(settings.get("state_updates", 1)),
        )

    @property
    def by_id(self) -> dict[str, StateFragment]:
        return {fragment.id: fragment for fragment in self.fragments}

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
        """The fragment rows ``direct_scene`` carries, in fragment order.

        Scene fragments, plus the one-value state fragments updated before the
        Writer. Those keep today's one-string parameter at the position a
        progressive fragment had, so a converted conversation's schema is
        unchanged.
        """
        riding = {fragment.id for fragment in self.director_values()}
        return [df for df in fragments if df.get("field_type") not in NON_SCENE_FIELD_TYPES or df.get("id") in riding]

    def to_director(self) -> tuple[StateFragment, ...]:
        return tuple(fragment for fragment in self.fragments if fragment.injects_director)

    def to_writer(self) -> tuple[StateFragment, ...]:
        return tuple(fragment for fragment in self.fragments if fragment.injects_writer)
