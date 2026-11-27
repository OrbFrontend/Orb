"""Where each decision asks the Judge, read off the fragment list, and the legacy orders that upgrade into it."""

import importlib
import sqlite3

from backend.database import upgrade_card_fragment_types
from backend.pipeline.passes.judge import CLOSING, OPENING, JudgePoint, JudgeTurn, judge_points
from backend.pipeline.passes.judge.render import DecisionSnapshot


def _row(fid: str, field_type: str = "string") -> dict:
    return {"id": fid, "label": fid.title(), "field_type": field_type}


def _decision(fid: str) -> dict:
    return _row(fid, "decision")


def test_a_decision_waits_for_its_place_only_when_its_situation_reads_what_changes_there():
    def decision(fid: str, situation: str) -> dict:
        return {**_decision(fid), "decision_state_template": situation}

    rows = [_row("attack"), decision("plan", "{{scene_guidance}}"), decision("state", "{{state}}"), _row("after")]
    plan = judge_points([*rows, decision("late_state", "{{state}}"), decision("late", "{{last_message}}")])
    assert plan.point_of("state") == JudgePoint("middle", "attack")
    # Between Director fragments the state is still the turn's start, so only the plan is worth waiting for.
    assert plan.asked_at("plan") == JudgePoint("middle", "attack") and plan.asked_at("state") == OPENING
    # After them the before-Writer state has changed too.
    assert plan.asked_at("late_state") == CLOSING and plan.asked_at("late") == OPENING
    assert plan.waits_after == {"attack"}


def test_list_order_sets_each_global_decisions_point():
    rows = [_decision("first"), _row("attack"), _row("hp", "state"), _decision("lands"), _decision("also"), _row("after")]
    plan = judge_points([*rows, _decision("last")])
    assert plan.by_decision == {
        "first": OPENING,
        "lands": JudgePoint("middle", "attack"),
        "also": JudgePoint("middle", "attack"),
        "last": CLOSING,
    }
    assert plan.labels == {"attack": "Attack"}


def test_card_decisions_are_placed_against_the_cards_own_director_fragments():
    rows = [_row("g1"), _row("g2"), _decision("c_first"), _row("c_attack"), _decision("c_lands"), _row("c_after"),
            _decision("c_last"), _decision("solo_card")]  # fmt: skip
    sources = {fid: "card" for fid in ("c_first", "c_attack", "c_lands", "c_after", "c_last")} | {"solo_card": "other"}
    plan = judge_points(rows, sources)
    # Without the per-source rule every card decision would sit after the global Director fragments.
    assert plan.point_of("c_first") == OPENING
    assert plan.point_of("c_lands") == JudgePoint("middle", "c_attack")
    assert plan.point_of("c_last") == CLOSING
    # A card with only decisions asks them ahead of the Director.
    assert plan.point_of("solo_card") == OPENING


def test_without_a_director_every_decision_is_asked_at_the_opening_point():
    from backend.core import parse_decision_definition
    from backend.pipeline.passes.judge import DecisionCandidate

    def candidate(fid: str, situation: str = "{{scene_guidance}}") -> DecisionCandidate:
        definition = parse_decision_definition(
            {
                "id": fid,
                "field_type": "decision",
                "decision_type": "noul",
                "decision_state_template": situation,
                "decision_instructions": "Q?",
                "decision_criteria": {"true": "a", "false": "b"},
                "decision_outputs": {"true": "", "false": ""},
                "decision_resolution": "threshold",
                "decision_threshold": 0.5,
            }
        )
        assert definition is not None
        return DecisionCandidate(definition)

    rows = [_decision("first"), _row("attack"), _decision("lands"), _row("after"), _decision("last")]
    situation = {"decision_state_template": "{{scene_guidance}}"}
    plan = judge_points([{**row, **situation} if row["field_type"] == "decision" else row for row in rows])
    turn = JudgeTurn(
        DecisionSnapshot(), (candidate("first", "{{last_message}}"), candidate("lands"), candidate("last")), plan=plan
    )
    assert [c.definition.fragment_id for c in turn.opening().candidates] == ["first"]
    assert turn.at(JudgePoint("middle", "attack"), "") is not None

    off = JudgeTurn(turn.snapshot, turn.candidates, plan=plan, director_runs=False)
    assert len(off.opening().candidates) == 3
    assert off.at(JudgePoint("middle", "attack"), "") is None and off.at(CLOSING, "") is None


def test_a_committed_result_keeps_its_middle_rulings_by_place():
    from backend.pipeline.passes.judge import JudgeResult

    def record(fid: str, inject: str, guidance: str, **point) -> dict:
        return {"fragment_id": fid, "injection_label": fid.title(), "inject": inject, "guidance": guidance, **point}

    committed = JudgeResult.committed(
        {
            "version": 2,
            "evaluations": [
                record("first", "both", "Settled.", point="opening"),
                record("lands", "director", "The lunge misses.", point="attack", point_label="Attack"),
                record("quiet", "writer", "Only for the Writer.", point="attack", point_label="Attack"),
                record("last", "writer", "After it all.", point="closing"),
            ],
        },
        {},
    )
    assert committed.rulings_after() == {"attack": [("Lands", "The lunge misses.")]}
    # A Director run again opens its plan with the opening outcome only.
    assert committed.opening_rulings() == [("First", "Settled.")]


def test_a_cards_legacy_runs_setting_becomes_its_place_in_the_list():
    entries = [
        {"id": "plan", "field_type": "string"},
        {"id": "early", "field_type": "decision", "decision_placement": "before_director"},
        {"id": "late", "field_type": "decision", "decision_placement": "after_director"},
        {"id": "next", "field_type": "string"},
        {"id": "kept", "field_type": "decision"},
    ]
    upgraded = upgrade_card_fragment_types({"orb": {"fragments": {"interactive": entries}}})["orb"]["fragments"]["interactive"]
    assert [entry["id"] for entry in upgraded] == ["early", "plan", "next", "late", "kept"]
    assert not any("decision_placement" in entry for entry in upgraded)


def test_the_migration_orders_legacy_decisions_and_drops_the_column():
    migration = importlib.import_module("backend.database.migrations.0099_decision_order")
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE interactive_fragments (id TEXT PRIMARY KEY, field_type TEXT, sort_order INTEGER, decision_placement TEXT)"
    )
    conn.executemany(
        "INSERT INTO interactive_fragments VALUES (?, ?, ?, ?)",
        [
            ("plan", "string", 0, None),
            ("late", "decision", 1, "after_director"),
            ("hp", "state", 2, None),
            ("next", "array", 3, None),
            ("early", "decision", 4, "before_director"),
            ("tips", "feedback", 5, None),
        ],
    )
    migration.migrate(conn)
    migration.migrate(conn)

    order = conn.execute("SELECT id, sort_order FROM interactive_fragments ORDER BY sort_order, id").fetchall()
    # The stage trades its own slots; the state and feedback rows keep theirs.
    assert order == [("early", 0), ("plan", 1), ("hp", 2), ("next", 3), ("late", 4), ("tips", 5)]
    assert "decision_placement" not in {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)")}
