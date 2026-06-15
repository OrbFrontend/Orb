"""Unit tests for the workflow registry: registration, tool diff, name
collisions, iteration order, and the overlay_enable_tools helper."""

from __future__ import annotations

from copy import deepcopy
from types import MappingProxyType

import pytest

from backend.tool_defs import STANDALONE_TOOLS, TOOLS
from backend.workflows import (
    HookType,
    Subscription,
    ToolNameCollision,
    Workflow,
    WorkflowMandateError,
    finalize_registry,
    get_subscription,
    get_workflow,
    iter_subscriptions,
    list_workflows,
    overlay_enable_tools,
    register_workflow,
    subscribe,
    workflow_has_hook,
)
from backend.workflows import registry as registry_module
from backend.workflows.contracts import ToolSpec


async def _noop_pre(ctx):  # type: ignore[no-untyped-def]
    if False:
        yield {}


async def _noop_post(ctx):  # type: ignore[no-untyped-def]
    if False:
        yield {}


async def _noop_on_demand(ctx, body):  # type: ignore[no-untyped-def]
    return {}


def _tool_spec(name: str, *, standalone: bool = True) -> ToolSpec:
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    choice = {"type": "function", "function": {"name": name}}
    return ToolSpec(name=name, schema=schema, choice=choice, standalone=standalone)


@pytest.fixture(autouse=True)
def _restore_globals():
    by_id_snapshot = {k: deepcopy(v) for k, v in registry_module._WORKFLOWS_BY_ID.items()}
    tools_snapshot = dict(TOOLS)
    standalone_snapshot = set(STANDALONE_TOOLS)
    # Tests below assert exact registry contents, so start from an empty
    # workflow registry rather than the first-party workflows registered at
    # import time. Built-in tools in TOOLS are left intact.
    registry_module._WORKFLOWS_BY_ID.clear()
    yield
    registry_module._WORKFLOWS_BY_ID.clear()
    registry_module._WORKFLOWS_BY_ID.update(by_id_snapshot)
    TOOLS.clear()
    TOOLS.update(tools_snapshot)
    STANDALONE_TOOLS.clear()
    STANDALONE_TOOLS.update(standalone_snapshot)


class TestFreshRegistration:
    def test_workflow_appears_in_list(self):
        register_workflow(Workflow(id="wf_a", display_name="A"))
        assert [w.id for w in list_workflows()] == ["wf_a"]

    def test_tool_lands_in_global_registry(self):
        spec = _tool_spec("wf_a_tool", standalone=True)
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[spec]))
        assert "wf_a_tool" in TOOLS
        assert "wf_a_tool" in STANDALONE_TOOLS

    def test_non_standalone_tool_omitted_from_standalone_set(self):
        spec = _tool_spec("wf_a_tool", standalone=False)
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[spec]))
        assert "wf_a_tool" in TOOLS
        assert "wf_a_tool" not in STANDALONE_TOOLS

    def test_get_workflow_returns_registered(self):
        w = Workflow(id="wf_a", display_name="A")
        register_workflow(w)
        assert get_workflow("wf_a") is w

    def test_get_workflow_missing(self):
        assert get_workflow("nope") is None


class TestReRegistration:
    def test_replace_in_place_keeps_list_length(self):
        register_workflow(Workflow(id="wf_a", display_name="A"))
        register_workflow(Workflow(id="wf_a", display_name="A v2"))
        wfs = list_workflows()
        assert len(wfs) == 1
        assert wfs[0].display_name == "A v2"

    def test_tool_diff_drop_removes_orphan(self):
        a = _tool_spec("ws_test_a")
        b = _tool_spec("ws_test_b")
        register_workflow(Workflow(id="ws_test", display_name="X", tools=[a, b]))
        assert "ws_test_a" in TOOLS and "ws_test_b" in TOOLS

        register_workflow(Workflow(id="ws_test", display_name="X", tools=[a]))
        assert "ws_test_a" in TOOLS
        assert "ws_test_b" not in TOOLS
        assert "ws_test_b" not in STANDALONE_TOOLS

    def test_tool_standalone_bit_toggles_symmetrically(self):
        spec_on = _tool_spec("wf_a_tool", standalone=True)
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[spec_on]))
        assert "wf_a_tool" in STANDALONE_TOOLS

        spec_off = _tool_spec("wf_a_tool", standalone=False)
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[spec_off]))
        assert "wf_a_tool" in TOOLS
        assert "wf_a_tool" not in STANDALONE_TOOLS


class TestBuiltinCollision:
    def test_raises_and_leaves_builtin_unchanged(self):
        before = TOOLS["editor_rewrite"]
        clash = _tool_spec("editor_rewrite")
        with pytest.raises(ToolNameCollision):
            register_workflow(Workflow(id="ws_clash", display_name="X", tools=[clash]))
        assert TOOLS["editor_rewrite"] is before
        assert get_workflow("ws_clash") is None

    def test_built_in_check_fires_before_cross_workflow_check(self):
        # Set up a workflow that legitimately owns a non-built-in tool.
        register_workflow(Workflow(id="wf_owner", display_name="O", tools=[_tool_spec("editor_rewrite_alt")]))
        # New workflow tries to claim a built-in name AND that name happens to
        # *also* live on another workflow's tools. The built-in check still
        # wins because it runs first.
        with pytest.raises(ToolNameCollision, match="built-in tool name"):
            register_workflow(Workflow(id="wf_other", display_name="X", tools=[_tool_spec("editor_rewrite")]))


class TestCrossWorkflowCollision:
    def test_two_workflows_cannot_share_tool(self):
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[_tool_spec("shared_tool")]))
        with pytest.raises(ToolNameCollision, match="owned by workflow 'wf_a'"):
            register_workflow(Workflow(id="wf_b", display_name="B", tools=[_tool_spec("shared_tool")]))
        assert get_workflow("wf_b") is None
        assert "shared_tool" in TOOLS

    def test_same_workflow_keeping_its_tool_no_collision(self):
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[_tool_spec("kept")]))
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[_tool_spec("kept")]))
        assert "kept" in TOOLS


class TestAtomicity:
    def test_rejected_registration_leaves_state_untouched(self):
        register_workflow(Workflow(id="wf_a", display_name="A", tools=[_tool_spec("kept")]))
        by_id_before = dict(registry_module._WORKFLOWS_BY_ID)
        tools_before = dict(TOOLS)
        standalone_before = set(STANDALONE_TOOLS)

        clash = Workflow(
            id="wf_b",
            display_name="B",
            tools=[_tool_spec("kept"), _tool_spec("new_one")],
        )
        with pytest.raises(ToolNameCollision):
            register_workflow(clash)

        assert registry_module._WORKFLOWS_BY_ID == by_id_before
        assert TOOLS == tools_before
        assert STANDALONE_TOOLS == standalone_before
        assert "new_one" not in TOOLS


class TestIterationOrder:
    def test_registration_order_preserved(self):
        register_workflow(Workflow(id="c", display_name="C"))
        register_workflow(Workflow(id="b", display_name="B"))
        register_workflow(Workflow(id="a", display_name="A"))
        ids = [w.id for w in list_workflows()]
        assert ids == ["c", "b", "a"]

    def test_re_registration_keeps_original_position(self):
        register_workflow(Workflow(id="z", display_name="Z"))
        register_workflow(Workflow(id="m", display_name="M"))
        register_workflow(Workflow(id="m", display_name="M2"))
        wfs = list_workflows()
        assert [w.id for w in wfs] == ["z", "m"]
        assert wfs[1].display_name == "M2"

    def test_list_returns_copy(self):
        register_workflow(Workflow(id="a", display_name="A"))
        wfs = list_workflows()
        wfs.append("not a workflow")  # type: ignore[arg-type]
        assert len(list_workflows()) == 1


class TestSubscriptionAPI:
    def test_subscribe_rejects_unknown_workflow(self):
        with pytest.raises(LookupError):
            subscribe("ghost", HookType.PRE_PIPELINE, _noop_pre)

    def test_subscribe_rejects_duplicate_pair(self):
        register_workflow(Workflow(id="dup", display_name="Dup"))
        subscribe("dup", HookType.POST_PIPELINE, _noop_post)
        with pytest.raises(ValueError):
            subscribe("dup", HookType.POST_PIPELINE, _noop_post)

    def test_subscribe_records_workflow_id_and_priority(self):
        register_workflow(Workflow(id="rec", display_name="Rec"))
        subscribe("rec", HookType.PRE_PIPELINE, _noop_pre, priority=7)
        sub = get_subscription("rec", HookType.PRE_PIPELINE)
        assert isinstance(sub, Subscription)
        assert sub.workflow_id == "rec"
        assert sub.priority == 7
        assert sub.callable is _noop_pre

    def test_iter_subscriptions_priority_ascending_insertion_tiebreak(self):
        register_workflow(Workflow(id="first", display_name="First"))
        register_workflow(Workflow(id="second", display_name="Second"))
        register_workflow(Workflow(id="third", display_name="Third"))
        subscribe("first", HookType.POST_PIPELINE, _noop_post, priority=10)
        subscribe("second", HookType.POST_PIPELINE, _noop_post, priority=0)
        subscribe("third", HookType.POST_PIPELINE, _noop_post, priority=0)
        ids = [s.workflow_id for s in iter_subscriptions(HookType.POST_PIPELINE)]
        assert ids == ["second", "third", "first"]

    def test_iter_subscriptions_filters_by_hook_type(self):
        register_workflow(Workflow(id="multi", display_name="Multi"))
        subscribe("multi", HookType.PRE_PIPELINE, _noop_pre)
        subscribe("multi", HookType.POST_PIPELINE, _noop_post)
        assert [s.hook_type for s in iter_subscriptions(HookType.PRE_PIPELINE)] == [HookType.PRE_PIPELINE]
        assert [s.hook_type for s in iter_subscriptions(HookType.POST_PIPELINE)] == [HookType.POST_PIPELINE]

    def test_iter_subscriptions_empty_when_no_match(self):
        register_workflow(Workflow(id="lonely", display_name="Lonely"))
        subscribe("lonely", HookType.PRE_PIPELINE, _noop_pre)
        assert iter_subscriptions(HookType.REGENERATE) == []

    def test_get_subscription_unknown_workflow_returns_none(self):
        assert get_subscription("ghost", HookType.REGENERATE) is None

    def test_get_subscription_missing_hook_returns_none(self):
        register_workflow(Workflow(id="onesided", display_name="OneSided"))
        subscribe("onesided", HookType.PRE_PIPELINE, _noop_pre)
        assert get_subscription("onesided", HookType.POST_PIPELINE) is None

    def test_workflow_has_hook_agrees_with_get_subscription(self):
        register_workflow(Workflow(id="probe", display_name="Probe"))
        subscribe("probe", HookType.ON_DEMAND, _noop_on_demand)
        w = get_workflow("probe")
        assert w is not None
        assert workflow_has_hook(w, HookType.ON_DEMAND) is True
        assert workflow_has_hook(w, HookType.REGENERATE) is False


class TestOverlayEnableTools:
    def test_none_contribution_returns_fresh_copy(self):
        base = {"x": True}
        out = overlay_enable_tools(base, None)
        assert out == base
        assert out is not base
        out["x"] = False
        assert base["x"] is True

    def test_set_contribution_enables_names(self):
        out = overlay_enable_tools({"x": False}, {"x", "y"})
        assert out == {"x": True, "y": True}

    def test_mapping_contribution_true_wins_false_ignored(self):
        out = overlay_enable_tools({"x": True, "y": False}, {"x": False, "y": True, "z": True})
        assert out == {"x": True, "y": True, "z": True}

    def test_accepts_mapping_proxy_base(self):
        base = MappingProxyType({"x": True})
        out = overlay_enable_tools(base, {"y"})
        assert isinstance(out, dict)
        assert out == {"x": True, "y": True}

    def test_empty_set_no_change(self):
        out = overlay_enable_tools({"x": True}, set())
        assert out == {"x": True}

    def test_frozenset_contribution(self):
        out = overlay_enable_tools({}, frozenset({"a", "b"}))
        assert out == {"a": True, "b": True}


class TestProducesArtifactsMandate:
    """Negative check in subscribe() + positive check in finalize_registry().

    Any ``produces_artifacts=True`` workflow must hold both ``REGENERATE`` and
    ``REROLL_GEN`` subscriptions. The negative check refuses binding either
    hook to a workflow that did not declare the flag; the positive check
    refuses a fully-wired registry that has the flag without both hooks.
    """

    async def _noop_regen(self, ctx, body):  # type: ignore[no-untyped-def]
        return []

    async def _noop_reroll(self, ctx, params, seed):  # type: ignore[no-untyped-def]
        return b""

    def test_subscribe_regenerate_rejected_when_produces_artifacts_false(self):
        register_workflow(Workflow(id="not_artifact", display_name="X", produces_artifacts=False))
        with pytest.raises(ValueError, match="produces_artifacts=True"):
            subscribe("not_artifact", HookType.REGENERATE, self._noop_regen)

    def test_subscribe_reroll_gen_rejected_when_produces_artifacts_false(self):
        register_workflow(Workflow(id="not_artifact", display_name="X", produces_artifacts=False))
        with pytest.raises(ValueError, match="produces_artifacts=True"):
            subscribe("not_artifact", HookType.REROLL_GEN, self._noop_reroll)

    def test_subscribe_regenerate_accepted_when_produces_artifacts_true(self):
        register_workflow(Workflow(id="art", display_name="Art", produces_artifacts=True))
        subscribe("art", HookType.REGENERATE, self._noop_regen)
        assert get_subscription("art", HookType.REGENERATE) is not None

    def test_subscribe_pre_pipeline_unaffected_by_produces_artifacts_flag(self):
        register_workflow(Workflow(id="non_art", display_name="N", produces_artifacts=False))
        subscribe("non_art", HookType.PRE_PIPELINE, _noop_pre)
        assert get_subscription("non_art", HookType.PRE_PIPELINE) is not None

    def test_finalize_registry_empty_registry_no_op(self):
        finalize_registry()

    def test_finalize_registry_non_producer_workflows_ignored(self):
        register_workflow(Workflow(id="x", display_name="X", produces_artifacts=False))
        register_workflow(Workflow(id="y", display_name="Y", produces_artifacts=False))
        finalize_registry()

    def test_finalize_registry_raises_when_regenerate_missing(self):
        register_workflow(Workflow(id="art", display_name="Art", produces_artifacts=True))
        subscribe("art", HookType.REROLL_GEN, self._noop_reroll)
        with pytest.raises(WorkflowMandateError, match="regenerate"):
            finalize_registry()

    def test_finalize_registry_raises_when_reroll_gen_missing(self):
        register_workflow(Workflow(id="art", display_name="Art", produces_artifacts=True))
        subscribe("art", HookType.REGENERATE, self._noop_regen)
        with pytest.raises(WorkflowMandateError, match="reroll_gen"):
            finalize_registry()

    def test_finalize_registry_passes_with_both_subscriptions(self):
        register_workflow(Workflow(id="art", display_name="Art", produces_artifacts=True))
        subscribe("art", HookType.REGENERATE, self._noop_regen)
        subscribe("art", HookType.REROLL_GEN, self._noop_reroll)
        finalize_registry()


class TestIsProducesArtifactsWorkflow:
    """The cache helper's predicate that gates byte writes by registry state."""

    def test_unregistered_id_returns_false(self):
        from backend.workflows.attachment_cache import _is_produces_artifacts_workflow

        assert _is_produces_artifacts_workflow("never_registered") is False

    def test_registered_without_flag_returns_false(self):
        from backend.workflows.attachment_cache import _is_produces_artifacts_workflow

        register_workflow(Workflow(id="x", display_name="X", produces_artifacts=False))
        assert _is_produces_artifacts_workflow("x") is False

    def test_registered_with_flag_returns_true(self):
        from backend.workflows.attachment_cache import _is_produces_artifacts_workflow

        register_workflow(Workflow(id="x", display_name="X", produces_artifacts=True))
        assert _is_produces_artifacts_workflow("x") is True

    def test_empty_string_returns_false(self):
        from backend.workflows.attachment_cache import _is_produces_artifacts_workflow

        assert _is_produces_artifacts_workflow("") is False
