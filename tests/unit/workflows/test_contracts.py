"""Unit tests for the boundary-contract layer: readonly_view wrapping and
frozen-dataclass behavior across the four Ctx classes."""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from backend.workflows.contracts import PostCtx, PreCtx, RerollGenCtx, ToolSpec, readonly_view


class TestReadonlyDict:
    def test_item_assignment_raises(self):
        wrapped = readonly_view({"a": 1})
        with pytest.raises(TypeError):
            wrapped["a"] = 2

    def test_nested_dict_wrapped(self):
        wrapped = readonly_view({"outer": {"inner": 1}})
        assert isinstance(wrapped["outer"], MappingProxyType)
        with pytest.raises(TypeError):
            wrapped["outer"]["inner"] = 99

    def test_mapping_proxy_passes_through(self):
        original = MappingProxyType({"a": 1})
        wrapped = readonly_view(original)
        # Idempotent: re-wrapping a MappingProxyType returns it unchanged
        # (the dict branch doesn't match -- MappingProxyType is not a dict).
        assert wrapped is original


class TestReadonlyListAndTuple:
    def test_list_becomes_tuple(self):
        wrapped = readonly_view([1, 2, 3])
        assert isinstance(wrapped, tuple)
        assert wrapped == (1, 2, 3)

    def test_append_raises(self):
        wrapped = readonly_view([1])
        with pytest.raises(AttributeError):
            wrapped.append(2)

    def test_nested_list_in_dict_raises(self):
        wrapped = readonly_view({"items": [1, 2]})
        with pytest.raises(AttributeError):
            wrapped["items"].append(3)


class TestReadonlySets:
    def test_set_becomes_frozenset(self):
        wrapped = readonly_view({1, 2})
        assert isinstance(wrapped, frozenset)
        assert wrapped == frozenset({1, 2})

    def test_add_raises(self):
        wrapped = readonly_view({1})
        with pytest.raises(AttributeError):
            wrapped.add(2)


class TestReadonlyBytes:
    def test_bytearray_becomes_bytes(self):
        wrapped = readonly_view(bytearray(b"abc"))
        assert isinstance(wrapped, bytes)
        assert wrapped == b"abc"


class TestReadonlyPrimitivesAndOpaque:
    def test_arbitrary_object_passthrough(self):
        obj = object()
        assert readonly_view(obj) is obj


class TestReadonlyDoesNotMutateSource:
    def test_source_dict_unchanged(self):
        src = {"a": 1, "nested": {"b": 2}}
        readonly_view(src)
        assert src == {"a": 1, "nested": {"b": 2}}


def _make_pre_ctx(history_src=None, settings_src=None) -> PreCtx:
    return PreCtx(
        conversation_id="c1",
        history=readonly_view(history_src or [{"role": "user", "content": "hi", "meta": {"k": "v"}}]),
        last_user_message="hi",
        settings=readonly_view(settings_src or {"a": 1, "nested": {"b": 2}}),
        prefix=readonly_view([{"role": "system", "content": "x"}]),
        enabled_tools_pre_merge=readonly_view({"editor_rewrite": True}),
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
            history=readonly_view([]),
            draft="d",
            effective_msg="m",
            director_output=readonly_view({}),
            settings=readonly_view({}),
            prefix=readonly_view([]),
            enabled_tools=readonly_view({}),
            turn_scratch={},
            client=object(),
            kv_tracker=object(),
            schema_overrides=MappingProxyType({}),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            post.draft = "e"  # type: ignore[misc]


class TestRerollGenCtxFields:
    """Pin RerollGenCtx field set: no history, no turn_scratch, no kv_tracker."""

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
            "replay",
        }

    def test_replay_defaults_to_reproducing(self):
        """The safe default: a ctx built without the field replays its stored
        parameters, which is what every caller did before the field existed."""
        rg = RerollGenCtx(
            conversation_id="c",
            message_id=1,
            attachment_id=2,
            original_attachment=readonly_view({}),
            settings=readonly_view({}),
            client=object(),
        )
        assert rg.replay is True

    def test_original_attachment_is_mapping_proxy_in_practice(self):
        rg = RerollGenCtx(
            conversation_id="c",
            message_id=1,
            attachment_id=2,
            original_attachment=readonly_view({"seed": "abc"}),
            settings=readonly_view({}),
            client=object(),
        )
        with pytest.raises(TypeError):
            rg.original_attachment["seed"] = "x"  # type: ignore[index]


class TestToolSpec:
    def test_defaults(self):
        spec = ToolSpec(name="x", schema={}, choice={})
        assert spec.standalone is True
