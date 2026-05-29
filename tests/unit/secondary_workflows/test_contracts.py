"""Unit tests for the boundary-contract layer: _readonly wrapping and
frozen-dataclass behavior across the four Ctx classes."""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from backend.secondary_workflows.contracts import (
    OnDemandCtx,
    PostCtx,
    PreCtx,
    RegenCtx,
    RerollGenCtx,
    ToolSpec,
    _readonly,
)


class TestReadonlyDict:
    def test_dict_becomes_mapping_proxy(self):
        wrapped = _readonly({"a": 1})
        assert isinstance(wrapped, MappingProxyType)

    def test_item_assignment_raises(self):
        wrapped = _readonly({"a": 1})
        with pytest.raises(TypeError):
            wrapped["a"] = 2

    def test_item_deletion_raises(self):
        wrapped = _readonly({"a": 1})
        with pytest.raises(TypeError):
            del wrapped["a"]

    def test_nested_dict_wrapped(self):
        wrapped = _readonly({"outer": {"inner": 1}})
        assert isinstance(wrapped["outer"], MappingProxyType)
        with pytest.raises(TypeError):
            wrapped["outer"]["inner"] = 99

    def test_mapping_proxy_passes_through(self):
        original = MappingProxyType({"a": 1})
        wrapped = _readonly(original)
        # Idempotent: re-wrapping a MappingProxyType returns it unchanged
        # (the dict branch doesn't match -- MappingProxyType is not a dict).
        assert wrapped is original


class TestReadonlyListAndTuple:
    def test_list_becomes_tuple(self):
        wrapped = _readonly([1, 2, 3])
        assert isinstance(wrapped, tuple)
        assert wrapped == (1, 2, 3)

    def test_append_raises(self):
        wrapped = _readonly([1])
        with pytest.raises(AttributeError):
            wrapped.append(2)

    def test_item_assignment_raises(self):
        wrapped = _readonly([1])
        with pytest.raises(TypeError):
            wrapped[0] = 2

    def test_tuple_stays_tuple(self):
        wrapped = _readonly((1, 2))
        assert isinstance(wrapped, tuple)

    def test_nested_list_in_dict_raises(self):
        wrapped = _readonly({"items": [1, 2]})
        with pytest.raises(AttributeError):
            wrapped["items"].append(3)


class TestReadonlySets:
    def test_set_becomes_frozenset(self):
        wrapped = _readonly({1, 2})
        assert isinstance(wrapped, frozenset)
        assert wrapped == frozenset({1, 2})

    def test_add_raises(self):
        wrapped = _readonly({1})
        with pytest.raises(AttributeError):
            wrapped.add(2)

    def test_frozenset_stays_frozenset(self):
        wrapped = _readonly(frozenset({1, 2}))
        assert isinstance(wrapped, frozenset)


class TestReadonlyBytes:
    def test_bytearray_becomes_bytes(self):
        wrapped = _readonly(bytearray(b"abc"))
        assert isinstance(wrapped, bytes)
        assert wrapped == b"abc"


class TestReadonlyPrimitivesAndOpaque:
    def test_string_passthrough(self):
        assert _readonly("hello") == "hello"

    def test_int_passthrough(self):
        assert _readonly(42) == 42

    def test_none_passthrough(self):
        assert _readonly(None) is None

    def test_arbitrary_object_passthrough(self):
        obj = object()
        assert _readonly(obj) is obj


class TestReadonlyDoesNotMutateSource:
    def test_source_dict_unchanged(self):
        src = {"a": 1, "nested": {"b": 2}}
        _readonly(src)
        assert src == {"a": 1, "nested": {"b": 2}}

    def test_source_list_unchanged(self):
        src = [1, 2, 3]
        _readonly(src)
        assert src == [1, 2, 3]


def _make_pre_ctx(history_src=None, settings_src=None) -> PreCtx:
    return PreCtx(
        conversation_id="c1",
        history=_readonly(history_src or [{"role": "user", "content": "hi", "meta": {"k": "v"}}]),
        last_user_message="hi",
        settings=_readonly(settings_src or {"a": 1, "nested": {"b": 2}}),
        prefix=_readonly([{"role": "system", "content": "x"}]),
        enabled_tools_pre_merge=_readonly({"editor_rewrite": True}),
        turn_scratch={},
        client=object(),
        kv_tracker=object(),
        schema_overrides=MappingProxyType({}),
    )


class TestPreCtx:
    def test_outer_list_mutation_raises(self):
        pre = _make_pre_ctx()
        with pytest.raises(AttributeError):
            pre.history.append({"role": "user"})
        with pytest.raises(TypeError):
            pre.history[0] = {"role": "system"}

    def test_outer_dict_mutation_raises(self):
        pre = _make_pre_ctx()
        with pytest.raises(TypeError):
            pre.settings["a"] = 2
        with pytest.raises(TypeError):
            del pre.settings["a"]
        with pytest.raises(TypeError):
            pre.enabled_tools_pre_merge["editor_apply_patch"] = True

    def test_nested_dict_mutation_raises(self):
        pre = _make_pre_ctx()
        with pytest.raises(TypeError):
            pre.history[0]["content"] = "x"
        with pytest.raises(TypeError):
            pre.settings["nested"]["b"] = 99
        with pytest.raises(TypeError):
            pre.history[0]["meta"]["k"] = "z"

    def test_reads_still_work(self):
        pre = _make_pre_ctx()
        assert pre.history[0]["role"] == "user"
        assert pre.settings["nested"]["b"] == 2
        assert pre.settings.get("a") == 1
        assert [m["role"] for m in pre.history] == ["user"]
        assert len(pre.history) == 1

    def test_two_instances_share_no_wrappers(self):
        src_history = [{"role": "user", "content": "hi"}]
        src_settings = {"a": 1}
        a = _make_pre_ctx(src_history, src_settings)
        b = _make_pre_ctx(src_history, src_settings)
        assert a.history is not b.history
        assert a.settings is not b.settings

    def test_frozen_reassignment_blocked(self):
        pre = _make_pre_ctx()
        with pytest.raises(dataclasses.FrozenInstanceError):
            pre.client = None  # type: ignore[misc]


class TestAllCtxFrozen:
    def test_postctx_frozen(self):
        post = PostCtx(
            conversation_id="c1",
            history=_readonly([]),
            draft="d",
            effective_msg="m",
            director_output=_readonly({}),
            settings=_readonly({}),
            prefix=_readonly([]),
            enabled_tools=_readonly({}),
            turn_scratch={},
            client=object(),
            kv_tracker=object(),
            schema_overrides=MappingProxyType({}),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            post.draft = "e"  # type: ignore[misc]

    def test_ondemandctx_frozen(self):
        od = OnDemandCtx(
            conversation_id="c1",
            history=_readonly([]),
            last_user_message="",
            settings=_readonly({}),
            client=object(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            od.client = None  # type: ignore[misc]

    def test_regenctx_frozen(self):
        rc = RegenCtx(
            conversation_id="c1",
            message_id=1,
            attachment_id=1,
            original_attachment=_readonly({}),
            history=_readonly([]),
            last_user_message="",
            settings=_readonly({}),
            client=object(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rc.client = None  # type: ignore[misc]

    def test_rerollgenctx_frozen(self):
        rg = RerollGenCtx(
            conversation_id="c1",
            message_id=1,
            attachment_id=1,
            original_attachment=_readonly({}),
            settings=_readonly({}),
            client=object(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            rg.client = None  # type: ignore[misc]


class TestRerollGenCtxFields:
    """Pin RerollGenCtx field set: no history, no turn_scratch, no kv_tracker."""

    def test_no_history_field(self):
        fields = {f.name for f in dataclasses.fields(RerollGenCtx)}
        assert "history" not in fields

    def test_no_turn_scratch_field(self):
        fields = {f.name for f in dataclasses.fields(RerollGenCtx)}
        assert "turn_scratch" not in fields

    def test_no_kv_tracker_field(self):
        fields = {f.name for f in dataclasses.fields(RerollGenCtx)}
        assert "kv_tracker" not in fields

    def test_expected_field_set(self):
        fields = {f.name for f in dataclasses.fields(RerollGenCtx)}
        assert fields == {
            "conversation_id",
            "message_id",
            "attachment_id",
            "original_attachment",
            "settings",
            "client",
            "prior_consumption_metadata",
        }

    def test_original_attachment_is_mapping_proxy_in_practice(self):
        rg = RerollGenCtx(
            conversation_id="c",
            message_id=1,
            attachment_id=2,
            original_attachment=_readonly({"seed": "abc"}),
            settings=_readonly({}),
            client=object(),
        )
        with pytest.raises(TypeError):
            rg.original_attachment["seed"] = "x"  # type: ignore[index]


class TestToolSpec:
    def test_defaults(self):
        spec = ToolSpec(name="x", schema={}, choice={})
        assert spec.standalone is True

    def test_can_set_standalone_false(self):
        spec = ToolSpec(name="x", schema={}, choice={}, standalone=False)
        assert spec.standalone is False
