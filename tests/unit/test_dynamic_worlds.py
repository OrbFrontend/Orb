"""Unit coverage for the Dynamic Worlds pure layer.

Three things are pure and therefore testable without a database:

* :func:`select_effective_entries` -- the authored/dynamic projection, which is
  what "an accepted change is visible" and "a reset restores the original" both
  reduce to;
* :func:`render_lorebook_block` and the constant/depth builders -- the section
  split that keeps Agent-managed state distinguishable in the prompt;
* :func:`validate_proposal` -- everything the model is *not* allowed to do.

Row shapes here are the same dicts the query layer returns, minus the columns
none of this reads.
"""

from __future__ import annotations

from backend.features.lorebook import (
    build_world_change_catalog,
    describe_operation,
    invert_operations,
    parse_proposal_call,
    split_by_world,
    validate_proposal,
)
from backend.inference.lorebook import (
    compute_constant_lorebook_block,
    compute_depth_lorebook_block,
    compute_lorebook_injection_block,
    render_lorebook_block,
    select_effective_entries,
)
from backend.pipeline.state import TurnState, WorldProposalTurn
from backend.pipeline.world_proposal import world_proposal_stage


def _authored(entry_id: int, name: str, content: str = "body", **kw) -> dict:
    row = {
        "id": entry_id,
        "world_id": "w1",
        "name": name,
        "content": content,
        "keywords": kw.pop("keywords", []),
        "constant": int(kw.pop("constant", False)),
        "at_depth": int(kw.pop("at_depth", False)),
        "priority": kw.pop("priority", 100),
        "sort_order": kw.pop("sort_order", 0),
        "enabled": 1,
        "entry_layer": "authored",
        "entry_revision": 0,
        "overlay_action": "",
        "supersedes_entry_id": None,
        "archived": 0,
    }
    row.update(kw)
    return row


def _dynamic(
    entry_id: int, name: str, action: str, target: int | None = None, **kw
) -> dict:
    row = _authored(entry_id, name, kw.pop("content", "body"), **kw)
    row.update(
        {
            "entry_layer": "dynamic",
            "overlay_action": action,
            "supersedes_entry_id": target,
        }
    )
    return row


async def test_proposal_stage_honours_a_world_disabled_during_the_turn(monkeypatch):
    """The launch-time tool blob is not permission to propose after opt-out."""
    import backend.pipeline.world_proposal as proposal_module

    async def disabled_world(_world_id):
        return {"id": "w1", "enabled": 1, "dynamic_enabled": 0, "content_revision": 7}

    async def entries_must_not_be_read(_world_id):
        raise AssertionError("a disabled world must stop before loading its entries")

    monkeypatch.setattr(proposal_module.db, "get_world", disabled_world)
    monkeypatch.setattr(
        proposal_module.db, "get_lorebook_entries", entries_must_not_be_read
    )
    state = TurnState(user_message="hello", resp_text="reply")
    turn = WorldProposalTurn(
        world_ids=("w1",), conversation_id="c1", user_message="hello"
    )

    events = [
        event
        async for event in world_proposal_stage(object(), state, settings={}, turn=turn)
    ]

    assert events == []
    assert state.world_proposals == []


async def test_proposal_stage_drops_only_the_world_that_opted_out(monkeypatch):
    """One World withdrawing mid-turn must not silence the others."""
    import backend.pipeline.world_proposal as proposal_module

    worlds = {
        "w1": {"id": "w1", "enabled": 1, "dynamic_enabled": 0, "content_revision": 1},
        "w2": {"id": "w2", "enabled": 0, "dynamic_enabled": 1, "content_revision": 2},
        "w3": {"id": "w3", "enabled": 1, "dynamic_enabled": 1, "content_revision": 3},
    }
    read: list[str] = []

    async def get_world(world_id):
        return worlds.get(world_id)

    async def get_entries(world_id):
        read.append(world_id)
        return [_authored(1, "A", world_id=world_id)]

    monkeypatch.setattr(proposal_module.db, "get_world", get_world)
    monkeypatch.setattr(proposal_module.db, "get_lorebook_entries", get_entries)

    loaded, entries = await proposal_module._load_targets(("w1", "w2", "w3"))

    assert [w["id"] for w in loaded] == ["w3"]
    assert read == ["w3"]
    assert [e["world_id"] for e in entries] == ["w3"]


# ── projection ────────────────────────────────────────────────────────────────


class TestEffectiveProjection:
    def test_a_world_with_no_overlay_is_unchanged(self):
        rows = [_authored(1, "A"), _authored(2, "B")]
        assert select_effective_entries(rows) == rows

    def test_rows_without_overlay_columns_read_as_authored(self):
        """Pre-migration rows and hand-built test dicts must pass straight through."""
        rows = [
            {"id": 1, "name": "A", "content": "x"},
            {"id": 2, "name": "B", "content": "y"},
        ]
        assert select_effective_entries(rows) == rows

    def test_addition_is_simply_included(self):
        rows = [_authored(1, "A"), _dynamic(9, "New", "add")]
        assert [e["id"] for e in select_effective_entries(rows)] == [1, 9]

    def test_replacement_hides_its_target_and_injects_itself(self):
        rows = [
            _authored(1, "Bridge", "stands"),
            _dynamic(9, "Bridge", "replace", 1, content="collapsed"),
        ]
        got = select_effective_entries(rows)
        assert [e["id"] for e in got] == [9]
        assert got[0]["content"] == "collapsed"

    def test_suppression_hides_its_target_and_injects_nothing(self):
        rows = [
            _authored(1, "Bridge"),
            _dynamic(9, "Bridge", "suppress", 1, content=""),
        ]
        assert select_effective_entries(rows) == []

    def test_archiving_the_overlay_re_exposes_the_authored_entry(self):
        """The whole basis of Reset to Authored World: no snapshot, just archiving."""
        overlay = _dynamic(9, "Bridge", "replace", 1, content="collapsed")
        rows = [_authored(1, "Bridge", "stands"), overlay]
        assert [e["id"] for e in select_effective_entries(rows)] == [9]
        overlay["archived"] = 1
        got = select_effective_entries(rows)
        assert [e["id"] for e in got] == [1]
        assert got[0]["content"] == "stands"

    def test_an_overlay_pointing_at_a_missing_target_still_injects(self):
        """A dangling supersedes_entry_id must not make the overlay vanish too."""
        rows = [_dynamic(9, "Ghost", "replace", 404, content="still here")]
        assert [e["id"] for e in select_effective_entries(rows)] == [9]


# ── rendering ─────────────────────────────────────────────────────────────────


class TestRendering:
    def test_dynamic_entries_render_after_authored_under_their_own_heading(self):
        block = render_lorebook_block(
            [_dynamic(9, "New", "add", content="fresh"), _authored(1, "Old", "settled")]
        )
        assert (
            block
            == "**Lorebook**\n\nOld: settled\n\n**Dynamic World State**\n\nNew: fresh"
        )

    def test_a_pure_authored_block_has_no_dynamic_heading(self):
        assert "Dynamic" not in render_lorebook_block([_authored(1, "Old", "settled")])

    def test_a_pure_dynamic_block_still_gets_its_heading_and_no_authored_one(self):
        block = render_lorebook_block([_dynamic(9, "New", "add", content="fresh")])
        assert block == "**Dynamic World State**\n\nNew: fresh"

    def test_priority_sorting_applies_within_a_section_not_across_them(self):
        """A high-priority dynamic entry still renders after every authored one."""
        rows = [
            _dynamic(9, "Hot", "add", content="d", priority=999),
            _authored(1, "Cold", "a", priority=1),
        ]
        block = render_lorebook_block(rows)
        assert block.index("Cold") < block.index("Hot")

    def test_constant_block_splits_sections_at_the_prefix_register(self):
        rows = [
            _authored(1, "Law", "gravity", constant=True),
            _dynamic(9, "Now", "add", content="raining", constant=True),
        ]
        block = compute_constant_lorebook_block(rows)
        assert (
            block
            == "## Lorebook\n\nLaw: gravity\n\n## Dynamic World State\n\nNow: raining"
        )

    def test_depth_block_splits_sections_too(self):
        rows = [
            _authored(1, "Law", "gravity", constant=True, at_depth=True),
            _dynamic(9, "Now", "add", content="raining", constant=True, at_depth=True),
        ]
        block = compute_depth_lorebook_block(rows)
        assert "**Lorebook (Depth)**" in block
        assert "**Dynamic World State (Depth)**" in block

    def test_a_suppressed_constant_entry_leaves_the_prefix(self):
        rows = [
            _authored(1, "Law", "gravity", constant=True),
            _dynamic(9, "Law", "suppress", 1, content=""),
        ]
        assert compute_constant_lorebook_block(rows) == ""

    def test_keyword_activation_works_for_a_dynamic_entry(self):
        """Hybrid activation: an accepted entry may ride keywords like any other."""
        rows = [
            _dynamic(9, "Mara", "add", content="Mara has a scar.", keywords=["Mara"])
        ]
        messages = [{"role": "user", "content": "I ask Mara about it"}]
        assert "Mara has a scar." in compute_lorebook_injection_block(messages, rows)
        miss = compute_lorebook_injection_block(
            [{"role": "user", "content": "nothing relevant"}], rows
        )
        assert miss == ""

    def test_a_replaced_entrys_keywords_no_longer_activate_it(self):
        rows = [
            _authored(1, "Bridge", "The bridge stands.", keywords=["bridge"]),
            _dynamic(
                9,
                "Bridge",
                "replace",
                1,
                content="The bridge collapsed.",
                keywords=["bridge"],
            ),
        ]
        block = compute_lorebook_injection_block(
            [{"role": "user", "content": "the bridge"}], rows
        )
        assert "collapsed" in block
        assert "stands" not in block


# ── proposal validation ───────────────────────────────────────────────────────


def _op(**kw) -> dict:
    base = {
        "op": "create",
        "name": "N",
        "content": "C",
        "activation": "constant",
        "rationale": "r",
        "evidence": "reply",
    }
    base.update(kw)
    return base


class TestValidateProposal:
    def test_empty_and_malformed_calls_propose_nothing(self):
        for arguments in (None, {}, {"operations": "not a list"}, {"operations": []}):
            assert validate_proposal(arguments, []).is_empty

    def test_a_clean_create_survives_with_its_rationale_and_evidence(self):
        result = validate_proposal(
            {"summary": "s", "operations": [_op(evidence="user")]}, []
        )
        assert result.summary == "s"
        assert result.operations == [
            {
                "op": "create",
                "rationale": "r",
                "evidence": "user",
                "name": "N",
                "content": "C",
                "activation": "constant",
                "keywords": [],
            }
        ]

    def test_unknown_evidence_falls_back_to_the_reply(self):
        (op,) = validate_proposal(
            {"operations": [_op(evidence="a dream")]}, []
        ).operations
        assert op["evidence"] == "reply"

    def test_keyword_activation_requires_a_keyword(self):
        result = validate_proposal(
            {"operations": [_op(activation="keywords", keywords=[])]}, []
        )
        assert result.is_empty
        assert "keyword" in result.rejected[0][1]

    def test_constant_activation_discards_keywords(self):
        (op,) = validate_proposal(
            {"operations": [_op(activation="constant", keywords=["x"])]}, []
        ).operations
        assert op["keywords"] == []

    def test_create_may_not_name_a_target(self):
        result = validate_proposal(
            {"operations": [_op(target_entry_id=1)]}, [_authored(1, "A")]
        )
        assert result.is_empty

    def test_replace_must_target_an_authored_entry(self):
        entries = [_authored(1, "A"), _dynamic(9, "D", "add")]
        ok = validate_proposal(
            {"operations": [_op(op="replace", target_entry_id=1)]}, entries
        )
        assert len(ok.operations) == 1
        bad = validate_proposal(
            {"operations": [_op(op="replace", target_entry_id=9)]}, entries
        )
        assert bad.is_empty and "authored" in bad.rejected[0][1]

    def test_update_and_archive_must_target_a_dynamic_entry(self):
        entries = [_authored(1, "A"), _dynamic(9, "D", "add")]
        assert validate_proposal(
            {"operations": [_op(op="update", target_entry_id=1)]}, entries
        ).is_empty
        assert validate_proposal(
            {"operations": [_op(op="archive", target_entry_id=1)]}, entries
        ).is_empty
        assert (
            len(
                validate_proposal(
                    {"operations": [_op(op="archive", target_entry_id=9)]}, entries
                ).operations
            )
            == 1
        )

    def test_the_agent_can_retire_a_suppression_to_bring_lore_back(self):
        """A suppression marker is invisible to the prompt but still targetable."""
        entries = [_authored(1, "A"), _dynamic(9, "A", "suppress", 1, content="")]
        result = validate_proposal(
            {"operations": [_op(op="archive", target_entry_id=9)]}, entries
        )
        assert len(result.operations) == 1

    def test_an_unknown_or_already_hidden_target_is_rejected(self):
        entries = [_authored(1, "A"), _dynamic(9, "A", "suppress", 1, content="")]
        assert validate_proposal(
            {"operations": [_op(op="replace", target_entry_id=404)]}, entries
        ).is_empty
        # 1 is hidden by the suppression, so it is no longer replaceable.
        assert validate_proposal(
            {"operations": [_op(op="replace", target_entry_id=1)]}, entries
        ).is_empty

    def test_two_operations_on_one_target_drop_the_second(self):
        entries = [_authored(1, "A"), _authored(2, "B")]
        result = validate_proposal(
            {
                "operations": [
                    _op(op="replace", target_entry_id=1),
                    _op(op="suppress", target_entry_id=1),
                ]
            },
            entries,
        )
        assert [o["op"] for o in result.operations] == ["replace"]
        assert "already targeted" in result.rejected[0][1]

    def test_a_duplicate_dynamic_name_is_ambiguous_and_rejected(self):
        entries = [_dynamic(9, "Mara", "add")]
        result = validate_proposal({"operations": [_op(name="Mara")]}, entries)
        assert result.is_empty and "already exists" in result.rejected[0][1]

    def test_two_creates_sharing_a_name_keep_only_the_first(self):
        result = validate_proposal(
            {"operations": [_op(name="Mara"), _op(name="mara")]}, []
        )
        assert len(result.operations) == 1

    def test_a_dynamic_entry_may_share_a_name_with_the_authored_one_it_replaces(self):
        entries = [_authored(1, "Bridge")]
        result = validate_proposal(
            {"operations": [_op(op="replace", target_entry_id=1, name="Bridge")]},
            entries,
        )
        assert len(result.operations) == 1

    def test_an_update_may_keep_its_own_name(self):
        entries = [_dynamic(9, "Mara", "add", content="old")]
        result = validate_proposal(
            {
                "operations": [
                    _op(op="update", target_entry_id=9, name="Mara", content="new")
                ]
            },
            entries,
        )
        assert result.operations[0]["content"] == "new"

    def test_an_update_that_omits_a_field_inherits_the_current_value(self):
        entries = [_dynamic(9, "Mara", "add", content="old body")]
        (op,) = validate_proposal(
            {
                "operations": [
                    _op(op="update", target_entry_id=9, name="", content="new body")
                ]
            },
            entries,
        ).operations
        assert op["name"] == "Mara" and op["content"] == "new body"

    def test_an_update_that_changes_nothing_is_rejected(self):
        entries = [_dynamic(9, "Mara", "add")]
        result = validate_proposal(
            {"operations": [{"op": "update", "target_entry_id": 9}]}, entries
        )
        assert result.is_empty

    def test_suppress_inherits_its_targets_name(self):
        entries = [_authored(1, "Bridge")]
        (op,) = validate_proposal(
            {
                "operations": [
                    {
                        "op": "suppress",
                        "target_entry_id": 1,
                        "rationale": "gone",
                        "evidence": "reply",
                    }
                ]
            },
            entries,
        ).operations
        assert op["name"] == "Bridge"

    def test_a_valid_operation_survives_alongside_a_rejected_one(self):
        result = validate_proposal(
            {"operations": [_op(op="update", target_entry_id=404), _op(name="Fine")]},
            [],
        )
        assert [o["name"] for o in result.operations] == ["Fine"]
        assert len(result.rejected) == 1


class TestParseProposalCall:
    def test_returns_none_when_the_model_called_something_else(self):
        assert parse_proposal_call([{"name": "direct_scene", "arguments": {}}]) is None
        assert parse_proposal_call([]) is None

    def test_the_last_matching_call_wins(self):
        calls = [
            {"name": "propose_world_changes", "arguments": {"summary": "first"}},
            {"name": "propose_world_changes", "arguments": {"summary": "second"}},
        ]
        assert parse_proposal_call(calls) == {"summary": "second"}


# ── catalog ───────────────────────────────────────────────────────────────────


class TestCatalog:
    def test_ids_are_the_real_row_ids_and_sections_are_labelled(self):
        entries = [_authored(1, "A", "alpha"), _dynamic(9, "B", "add", content="beta")]
        catalog = build_world_change_catalog(entries)
        assert "### Authored" in catalog and "### Dynamic World State" in catalog
        assert "- [1] A" in catalog and "- [9] B" in catalog

    def test_dynamic_entries_always_carry_full_content(self):
        long_body = "x" * 300
        catalog = build_world_change_catalog(
            [_dynamic(9, "B", "add", content=long_body)]
        )
        assert long_body in catalog

    def test_an_authored_entry_is_elided_until_the_exchange_makes_it_relevant(self):
        long_body = "y" * 300
        entry = _authored(1, "Bridge", long_body, keywords=["bridge"])
        assert long_body not in build_world_change_catalog(
            [entry], exchange_text="unrelated chatter"
        )
        assert long_body in build_world_change_catalog(
            [entry], exchange_text="we crossed the bridge"
        )

    def test_hidden_lore_is_absent_from_the_catalog(self):
        entries = [
            _authored(1, "Bridge"),
            _dynamic(9, "Bridge", "suppress", 1, content=""),
        ]
        assert build_world_change_catalog(entries) == ""

    def test_an_empty_world_yields_an_empty_catalog(self):
        assert build_world_change_catalog([]) == ""


# ── several worlds in one call ────────────────────────────────────────────────


def _world(world_id: str, name: str) -> dict:
    return {"id": world_id, "name": name, "enabled": 1, "dynamic_enabled": 1}


class TestMultiWorldCatalog:
    def test_each_world_is_named_so_a_create_can_address_it(self):
        entries = [
            _authored(1, "Bridge", world_id="w1"),
            _authored(2, "Ledger", world_id="w2"),
        ]
        catalog = build_world_change_catalog(
            entries, worlds=[_world("w1", "Gorge"), _world("w2", "Guild")]
        )
        assert "## Gorge" in catalog and "## Guild" in catalog
        assert catalog.index("## Gorge") < catalog.index("- [1] Bridge")
        assert catalog.index("## Guild") < catalog.index("- [2] Ledger")

    def test_a_world_with_nothing_in_it_is_still_listed(self):
        """It is a legal `target_world` for a create, so it has to be visible."""
        catalog = build_world_change_catalog(
            [_authored(1, "Bridge", world_id="w1")],
            worlds=[_world("w1", "Gorge"), _world("w2", "Guild")],
        )
        assert "## Guild" in catalog and "(no entries yet)" in catalog

    def test_naming_no_worlds_keeps_the_flat_single_world_shape(self):
        catalog = build_world_change_catalog([_authored(1, "Bridge", world_id="w1")])
        assert "##" not in catalog.replace("###", "")


class TestMultiWorldValidation:
    _WORLDS = [_world("w1", "Gorge"), _world("w2", "Guild")]

    def test_a_create_lands_in_the_world_it_names(self):
        (op,) = validate_proposal(
            {"operations": [_op(target_world="Guild")]}, [], worlds=self._WORLDS
        ).operations
        assert op["world_id"] == "w2"

    def test_a_world_may_be_named_case_insensitively_or_by_id(self):
        for named in ("gorge", "GORGE", "w1"):
            (op,) = validate_proposal(
                {"operations": [_op(target_world=named)]}, [], worlds=self._WORLDS
            ).operations
            assert op["world_id"] == "w1"

    def test_a_create_naming_no_world_is_dropped_when_there_is_a_choice(self):
        result = validate_proposal({"operations": [_op()]}, [], worlds=self._WORLDS)
        assert result.is_empty and "target_world" in result.rejected[0][1]

    def test_a_create_naming_an_unknown_world_is_dropped(self):
        result = validate_proposal(
            {"operations": [_op(target_world="Atlantis")]}, [], worlds=self._WORLDS
        )
        assert result.is_empty and "unknown target_world" in result.rejected[0][1]

    def test_two_worlds_sharing_a_name_address_neither(self):
        """Resolving it would write to whichever happened to sort first."""
        result = validate_proposal(
            {"operations": [_op(target_world="Twin")]},
            [],
            worlds=[_world("w1", "Twin"), _world("w2", "Twin")],
        )
        assert result.is_empty

    def test_the_only_world_needs_no_naming(self):
        (op,) = validate_proposal(
            {"operations": [_op()]}, [], worlds=[_world("w1", "Gorge")]
        ).operations
        assert op["world_id"] == "w1"

    def test_a_targeted_operation_takes_the_world_of_the_row_it_names(self):
        """Entry ids are globally unique, so this cannot be misdirected."""
        entries = [
            _authored(1, "Bridge", world_id="w1"),
            _authored(2, "Ledger", world_id="w2"),
        ]
        (op,) = validate_proposal(
            # The wrong world named outright: the row still decides.
            {
                "operations": [
                    _op(op="replace", target_entry_id=2, target_world="Gorge")
                ]
            },
            entries,
            worlds=self._WORLDS,
        ).operations
        assert op["world_id"] == "w2"

    def test_the_same_name_may_exist_in_two_worlds(self):
        entries = [_dynamic(9, "Ledger", "add", world_id="w1")]
        ops = validate_proposal(
            {
                "operations": [
                    _op(name="Ledger", target_world="Guild"),
                    _op(name="Ledger", target_world="Gorge"),
                ]
            },
            entries,
            worlds=self._WORLDS,
        )
        # The first lands in the world that has no Ledger; the second collides.
        assert [o["world_id"] for o in ops.operations] == ["w2"]
        assert "already exists" in ops.rejected[0][1]

    def test_re_validating_one_world_leaves_operations_unstamped(self):
        """The accept path already knows its World; a stamp would only be noise."""
        (op,) = validate_proposal(
            {"operations": [_op()]}, [_authored(1, "A", world_id="w1")]
        ).operations
        assert "world_id" not in op


class TestSplitByWorld:
    def test_operations_are_grouped_and_the_stamp_comes_off(self):
        grouped = split_by_world(
            [
                {"op": "create", "world_id": "w2", "name": "B"},
                {"op": "create", "world_id": "w1", "name": "A"},
                {"op": "create", "world_id": "w2", "name": "C"},
            ]
        )
        assert list(grouped) == ["w2", "w1"]
        assert grouped["w2"] == [
            {"op": "create", "name": "B"},
            {"op": "create", "name": "C"},
        ]

    def test_an_unstamped_operation_has_no_world_to_be_filed_under(self):
        assert split_by_world([{"op": "create", "name": "A"}]) == {}


# ── inverse operations ────────────────────────────────────────────────────────


class TestInvertOperations:
    def test_a_created_row_is_undone_by_archiving_it(self):
        after = {"id": 9, "archived": 0}
        inverse, required = invert_operations([{"op": "create"}], [None], [after])
        assert inverse == [{"op": "archive", "target_entry_id": 9, "archived": True}]
        assert required == [after]

    def test_an_update_is_undone_by_restoring_the_before_values(self):
        before = {
            "id": 9,
            "name": "Old",
            "content": "old",
            "keywords": ["k"],
            "constant": 0,
            "priority": 100,
            "enabled": 1,
        }
        after = {"id": 9, "name": "New", "content": "new", "entry_revision": 1}
        (inverse,), (state,) = invert_operations(
            [{"op": "update", "target_entry_id": 9}], [before], [after]
        )
        assert (
            inverse["name"] == "Old"
            and inverse["content"] == "old"
            and inverse["activation"] == "keywords"
        )
        assert state == after

    def test_an_archive_is_undone_by_restoring_the_before_flag(self):
        before, after = {"id": 9, "archived": 0}, {"id": 9, "archived": 1}
        (inverse,), _ = invert_operations(
            [{"op": "archive", "target_entry_id": 9}], [before], [after]
        )
        assert inverse == {"op": "archive", "target_entry_id": 9, "archived": False}

    def test_operations_unwind_in_reverse_order(self):
        ops = [{"op": "create"}, {"op": "create"}]
        inverse, _ = invert_operations(ops, [None, None], [{"id": 9}, {"id": 10}])
        assert [i["target_entry_id"] for i in inverse] == [10, 9]

    def test_an_operation_that_produced_no_row_is_skipped(self):
        assert invert_operations([{"op": "create"}], [None], [None]) == ([], [])


def test_describe_operation_names_its_target():
    by_id = {1: {"name": "Bridge"}}
    assert (
        describe_operation({"op": "suppress", "target_entry_id": 1}, by_id)
        == "Suppress Bridge [1]"
    )
    assert describe_operation({"op": "create", "name": "Scar"}, by_id) == "Add “Scar”"
