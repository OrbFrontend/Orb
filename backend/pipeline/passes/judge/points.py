"""Where in the turn each decision asks the Judge, read off the fragment list order."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from ....core import DECISION_FIELD_TYPE, is_director_fragment
from .render import macros_used


class JudgePoint(NamedTuple):
    """One place the Judge is asked: ahead of the Director, after one of its fragments, or after the whole plan."""

    kind: Literal["opening", "middle", "closing"]
    # The Director fragment a middle point follows.
    after: str = ""


OPENING = JudgePoint("opening")
CLOSING = JudgePoint("closing")

# What a decision's Situation must read for its question to change between the opening point and its place: between
# Director fragments only the plan does, after the last one the plan and the before-Writer state.
_WAITS_FOR = {"middle": frozenset({"scene_guidance"}), "closing": frozenset({"scene_guidance", "state"})}


@dataclass(frozen=True, slots=True)
class JudgePlan:
    """Each decision's place in the run and the point it is asked at, and the label of every Director fragment a middle
    point follows.

    A decision whose Situation reads nothing that changes before its place renders there exactly as at the opening point,
    so it is asked with the opening point: one Judge request instead of a later one, and no wait between Director steps.
    Its outcome still reaches only what its place reaches.
    """

    by_decision: Mapping[str, JudgePoint] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    asked: Mapping[str, JudgePoint] = field(default_factory=dict)

    def point_of(self, fragment_id: str) -> JudgePoint:
        """Where *fragment_id* sits in the run, which says what its outcome reaches."""
        return self.by_decision.get(fragment_id, OPENING)

    def asked_at(self, fragment_id: str) -> JudgePoint:
        """The point the Judge asks *fragment_id* at: its place, or the opening point when nothing it reads changes."""
        return self.asked.get(fragment_id, self.point_of(fragment_id))

    @property
    def waits_after(self) -> frozenset[str]:
        """The Director fragments the Director stops after, for a decision that reads the plan so far."""
        return frozenset(point.after for point in self.asked.values() if point.kind == "middle")


def judge_points(rows: Sequence[Mapping[str, Any]], sources: Mapping[str, str] | None = None) -> JudgePlan:
    """The point of each decision in *rows*, the fragments in run order; *sources* maps a card row's id to its card.

    Per source (the globals, each card), a decision ahead of that source's first Director fragment joins the opening point
    and one after its last joins the closing point; the rest run after the Director fragment just above them. Cards follow
    every global in the list, so without this rule each card decision would land after the global Director fragments.
    """
    sources = sources or {}

    def source(row: Mapping[str, Any]) -> str:
        return sources.get(str(row["id"]), "")

    marks: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if is_director_fragment(str(row.get("field_type") or "")):
            marks.setdefault(source(row), []).append(index)
    by_decision: dict[str, JudgePoint] = {}
    asked: dict[str, JudgePoint] = {}
    labels: dict[str, str] = {}
    above: Mapping[str, Any] | None = None
    for index, row in enumerate(rows):
        field_type = str(row.get("field_type") or "")
        if is_director_fragment(field_type):
            above = row
            continue
        if field_type != DECISION_FIELD_TYPE:
            continue
        own = marks.get(source(row), [])
        if not own or index < own[0]:
            point = OPENING
        elif index > own[-1]:
            point = CLOSING
        else:
            assert above is not None
            point = JudgePoint("middle", str(above["id"]))
            labels[point.after] = str(above.get("label") or above.get("injection_label") or above["id"])
        by_decision[str(row["id"])] = point
        if point != OPENING:
            reads = set(macros_used(str(row.get("decision_state_template") or "")))
            asked[str(row["id"])] = point if reads & _WAITS_FOR[point.kind] else OPENING
    return JudgePlan(by_decision, labels, asked)


def point_name(point: JudgePoint) -> str:
    """*point* as a record names it: ``opening``, ``closing``, or the id of the Director fragment it follows."""
    return point.after if point.kind == "middle" else point.kind
