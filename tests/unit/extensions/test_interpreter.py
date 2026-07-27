"""The flow interpreter: resolution, quotas, staging, and the grant re-check.

These run without a database or an HTTP client. The interpreter reaches the
outside world only through :class:`HostServices`, so a test supplies that record
and gets full control of what "the outside world" answers -- which is the same
property that makes the reachable surface reviewable in production.

The assertions cluster around the three claims the design makes about an
invocation: nothing is applied until the flow returns, every bound is enforced
at its own boundary, and the *live* grant view is consulted immediately before
each privileged operation rather than captured at the start.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.contracts import Flow, OpContext, parse_schema
from backend.features.extensions.errors import FlowCancelled, FlowError
from backend.features.extensions.interpreter import (
    DELETED,
    FlowResult,
    HostServices,
    Invocation,
    run_flow,
)
from backend.features.extensions.limits import (
    MAX_FLOW_STEPS_DECLARED,
    MAX_FLOW_STEPS_EXECUTED,
    MAX_JSON_STRING_BYTES,
    MAX_STATE_BYTES_PER_SCOPE,
)
from backend.features.extensions.values import (
    MISSING,
    evaluate_predicate,
    render_template,
    resolve_path,
)

ALL_GRANTS = frozenset(
    {
        ("state.read", None),
        ("state.read", "conversation"),
        ("state.write", None),
        ("state.write", "conversation"),
        ("context.draft.read", None),
        ("prompt.context.append", None),
        ("prompt.context.append", "writer"),
        ("draft.replace", None),
        ("ui.contribute", None),
        ("model.call", None),
        ("model.call", "agent"),
    }
)


def flow(*steps) -> Flow:
    return Flow.model_validate({"flow_version": 1, "steps": list(steps)})


async def run(
    f: Flow,
    *,
    ctx: dict | None = None,
    state: dict | None = None,
    grants=ALL_GRANTS,
    context: OpContext = OpContext.ACTION,
    action_input: dict | None = None,
    is_cancelled=lambda: False,
    output_schema=None,
) -> tuple[FlowResult, list[dict]]:
    """Execute *f* and return its result plus the progress events it yielded."""
    stored = dict(state or {})

    async def read_state(scope: str):
        return stored

    invocation = Invocation(
        extension_id="scene-meter",
        context=context,
        host=HostServices(grants=lambda: grants, read_state=read_state, is_cancelled=is_cancelled),
        ctx=ctx or {},
        action_input=action_input or {},
        scopes_in_scope=frozenset({"conversation", "config"}),
        seed="test",
        output_schema=parse_schema(output_schema) if output_schema else None,
    )
    events: list[dict] = []
    result: FlowResult | None = None
    async for item in run_flow(f, invocation):
        if isinstance(item, FlowResult):
            result = item
        else:
            events.append(item)
    assert result is not None
    return result, events


# ── value resolution ────────────────────────────────────────────────────────


def test_a_missing_path_resolves_to_the_sentinel_not_to_none():
    """``null`` stays a storable value; absence is a different thing entirely."""
    namespaces = {"ctx": {"draft": None}}
    assert resolve_path(namespaces, "ctx.draft") is None
    assert resolve_path(namespaces, "ctx.nope") is MISSING


def test_resolution_walks_mappings_and_indices_but_never_attributes():
    namespaces = {"ctx": {"history": [{"role": "user"}]}}
    assert resolve_path(namespaces, "ctx.history.0.role") == "user"
    assert resolve_path(namespaces, "ctx.history.9") is MISSING
    assert resolve_path(namespaces, "ctx.draft.__class__") is MISSING


def test_exists_distinguishes_a_null_value_from_an_absent_path():
    namespaces = {"ctx": {"draft": None}}
    assert evaluate_predicate({"exists": {"$ref": "ctx.draft"}}, namespaces) is True
    assert evaluate_predicate({"exists": {"$ref": "ctx.nope"}}, namespaces) is False


def test_comparisons_are_type_strict():
    """``1 == True`` is a Python quirk, not a rule a package author can follow."""
    namespaces = {"ctx": {"n": 1, "flag": True, "text": "1"}}
    assert evaluate_predicate({"eq": [{"$ref": "ctx.n"}, 1]}, namespaces) is True
    assert evaluate_predicate({"eq": [{"$ref": "ctx.flag"}, 1]}, namespaces) is False
    assert evaluate_predicate({"eq": [{"$ref": "ctx.text"}, 1]}, namespaces) is False
    # Incomparable operands are False rather than an error: a guard must never
    # be the thing that fails the flow it was meant to protect.
    assert evaluate_predicate({"lt": [{"$ref": "ctx.text"}, 1]}, namespaces) is False


def test_a_template_substitutes_scalars_and_refuses_gaps():
    namespaces = {"ctx": {"n": 7, "obj": {"a": 1}}}
    assert render_template("value {{ctx.n}}", namespaces) == "value 7"
    with pytest.raises(FlowError):
        render_template("{{ctx.missing}}", namespaces)
    with pytest.raises(FlowError):
        render_template("{{ctx.obj}}", namespaces)


async def test_the_missing_sentinel_cannot_be_returned():
    with pytest.raises(FlowError):
        await run(flow({"op": "return", "value": {"$ref": "ctx.absent"}}))


# ── effects are staged, never applied ───────────────────────────────────────


async def test_state_writes_are_staged_and_folded_in_program_order():
    result, _ = await run(
        flow(
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "tension",
                "value": 10,
            },
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "tension",
                "value": 40,
            },
            {"op": "state.delete", "scope": "conversation", "path": "stale"},
        ),
        state={"stale": "x"},
    )
    assert result.effects.state["conversation"] == {"tension": 40, "stale": DELETED}


async def test_a_failure_after_a_staged_write_leaves_nothing_staged_for_the_caller():
    """The caller commits ``effects``; a raise means it never gets one."""
    with pytest.raises(FlowError):
        await run(
            flow(
                {
                    "op": "state.set",
                    "scope": "conversation",
                    "path": "tension",
                    "value": 40,
                },
                {"op": "math.add", "a": {"$ref": "ctx.nope"}, "b": 1},
            )
        )


async def test_a_flow_reads_back_its_own_staged_write():
    result, _ = await run(
        flow(
            {"op": "state.set", "scope": "conversation", "path": "n", "value": 5},
            {"id": "read", "op": "state.get", "scope": "conversation", "path": "n"},
            {"op": "return", "value": {"$ref": "steps.read"}},
        )
    )
    assert result.value == 5


async def test_a_staged_delete_reads_back_as_absent():
    result, _ = await run(
        flow(
            {"op": "state.delete", "scope": "conversation", "path": "n"},
            {"id": "read", "op": "state.get", "scope": "conversation", "path": "n"},
            {
                "op": "return",
                "value": "gone",
                "when": {"not": {"exists": {"$ref": "steps.read"}}},
            },
            {"op": "return", "value": "still-there"},
        ),
        state={"n": 1},
    )
    assert result.value == "gone"


# ── quotas and bounds ───────────────────────────────────────────────────────


async def test_the_state_slot_cap_measures_the_resulting_slot_not_the_delta():
    """Filling a slot one small write at a time must hit the same wall."""
    chunk = "x" * 40_000
    steps = [{"op": "state.set", "scope": "conversation", "path": f"k{i}", "value": chunk} for i in range(10)]
    with pytest.raises(FlowError, match=f"over the {MAX_STATE_BYTES_PER_SCOPE} byte limit"):
        await run(flow(*steps))


async def test_runtime_json_strings_use_the_same_bound_as_package_json():
    with pytest.raises(FlowError, match=f"string over the {MAX_JSON_STRING_BYTES} byte limit"):
        await run(flow({"op": "return", "value": "x" * (MAX_JSON_STRING_BYTES + 1)}))


async def test_a_second_draft_replacement_exceeds_its_budget():
    with pytest.raises(FlowError, match="budget of 1 draft replacements"):
        await run(
            flow(
                {"op": "draft.replace", "value": "one"},
                {"op": "draft.replace", "value": "two"},
            ),
            context=OpContext.POST_TRANSFORM,
        )


async def test_an_oversized_context_block_is_refused_at_the_operation():
    with pytest.raises(FlowError, match="over the 8192 byte limit"):
        await run(
            flow(
                {
                    "op": "context.append",
                    "targets": ["writer"],
                    "label": "L",
                    "text": "y" * 9000,
                }
            ),
            context=OpContext.PRE_PIPELINE,
        )


async def test_arithmetic_rejects_a_non_number():
    with pytest.raises(FlowError, match="is not a number"):
        await run(flow({"op": "math.add", "a": "seven", "b": 1}))


# ── control flow ────────────────────────────────────────────────────────────


async def test_an_if_runs_exactly_one_branch():
    result, _ = await run(
        flow(
            {
                "op": "if",
                "when": {"gt": [{"$ref": "ctx.n"}, 5]},
                "then": [{"op": "return", "value": "high"}],
                "else": [{"op": "return", "value": "low"}],
            }
        ),
        ctx={"n": 9},
    )
    assert result.value == "high"


async def test_an_if_runs_its_else_branch_when_the_condition_is_false():
    result, _ = await run(
        flow(
            {
                "op": "if",
                "when": {"gt": [{"$ref": "ctx.n"}, 5]},
                "then": [{"op": "return", "value": "high"}],
                "else": [{"op": "return", "value": "low"}],
            }
        ),
        ctx={"n": 2},
    )
    assert result.value == "low"


async def test_finite_float_is_a_valid_runtime_json_number():
    result, _ = await run(flow({"op": "return", "value": 1.5}))
    assert result.value == 1.5


async def test_falling_off_the_end_returns_null():
    result, _ = await run(flow({"op": "state.set", "scope": "conversation", "path": "n", "value": 1}))
    assert result.value is None


async def test_on_error_continue_substitutes_its_declared_fallback():
    result, _ = await run(
        flow(
            {
                "id": "risky",
                "op": "math.add",
                "a": {"$ref": "ctx.absent"},
                "b": 1,
                "on_error": "continue",
                "fallback": 0,
            },
            {"op": "return", "value": {"$ref": "steps.risky"}},
        )
    )
    assert result.value == 0


async def test_on_error_continue_does_not_swallow_cancellation():
    """A cancelled turn stops the flow; it is not a step that can be recovered."""
    with pytest.raises(FlowCancelled):
        await run(
            flow(
                {
                    "op": "math.add",
                    "a": 1,
                    "b": 1,
                    "on_error": "continue",
                    "fallback": 0,
                }
            ),
            is_cancelled=lambda: True,
        )


async def test_the_declaration_bound_keeps_the_execution_bound_unreachable():
    """A loop-free language means declared steps bound executed steps.

    The runtime counter is therefore defense in depth rather than the primary
    gate -- worth asserting explicitly, because the day someone adds an
    iterating construct is the day the two numbers stop being interchangeable
    and the runtime counter becomes the only one that still holds.
    """
    assert MAX_FLOW_STEPS_DECLARED <= MAX_FLOW_STEPS_EXECUTED
    result, _ = await run(flow(*[{"op": "math.add", "a": 1, "b": 1} for _ in range(MAX_FLOW_STEPS_DECLARED)]))
    assert result.value is None
    with pytest.raises(ValidationError):
        flow(*[{"op": "math.add", "a": 1, "b": 1} for _ in range(MAX_FLOW_STEPS_DECLARED + 1)])


# ── permissions ─────────────────────────────────────────────────────────────


async def test_a_privileged_operation_fails_when_its_grant_is_absent():
    with pytest.raises(FlowError, match="permission state.write for 'conversation' is not granted"):
        await run(
            flow({"op": "state.set", "scope": "conversation", "path": "n", "value": 1}),
            grants=frozenset({("state.read", "conversation")}),
        )


async def test_revoking_mid_flow_stops_the_next_operation():
    """The grant view is a callable, so revocation reaches a running flow.

    This is the one place snapshot isolation is deliberately given up: a
    disable or an update leaves an in-flight invocation alone, but a revoked
    permission must stop the very next privileged step.
    """
    live = {"grants": ALL_GRANTS}

    def revoke_after_first():
        current = live["grants"]
        live["grants"] = frozenset()
        return current

    stored: dict = {}

    async def read_state(scope):
        return stored

    invocation = Invocation(
        extension_id="scene-meter",
        context=OpContext.ACTION,
        host=HostServices(grants=revoke_after_first, read_state=read_state),
        scopes_in_scope=frozenset({"conversation"}),
    )
    body = flow(
        {"op": "state.set", "scope": "conversation", "path": "a", "value": 1},
        {"op": "state.set", "scope": "conversation", "path": "b", "value": 2},
    )
    with pytest.raises(FlowError, match="is not granted"):
        async for _ in run_flow(body, invocation):
            pass


async def test_a_scope_with_no_entity_in_this_invocation_is_refused():
    stored: dict = {}

    async def read_state(scope):
        return stored

    invocation = Invocation(
        extension_id="scene-meter",
        context=OpContext.ACTION,
        host=HostServices(grants=lambda: ALL_GRANTS, read_state=read_state),
        scopes_in_scope=frozenset(),
    )
    with pytest.raises(FlowError, match="no conversation in scope"):
        async for _ in run_flow(flow({"op": "state.get", "scope": "conversation", "path": "n"}), invocation):
            pass


# ── build-gated operations ──────────────────────────────────────────────────


async def test_an_operation_this_build_cannot_run_is_refused_rather_than_half_done():
    with pytest.raises(FlowError, match="not available in this Orb build"):
        await run(
            flow(
                {
                    "op": "http.request",
                    "method": "GET",
                    "url": "https://example.invalid/x",
                }
            ),
        )


# ── determinism and progress ────────────────────────────────────────────────


async def test_random_is_per_invocation_seeded_and_reproducible():
    body = flow(
        {"id": "roll", "op": "random.integer", "minimum": 0, "maximum": 1_000_000},
        {"op": "return", "value": {"$ref": "steps.roll"}},
    )
    first, _ = await run(body)
    second, _ = await run(body)
    assert first.value == second.value


async def test_ui_status_streams_as_progress_and_is_not_an_effect():
    result, events = await run(
        flow(
            {"op": "ui.status", "text": "scoring"},
            {"op": "ui.invalidate", "view": "inspector"},
        ),
    )
    assert events == [{"type": "status", "text": "scoring"}]
    assert result.effects.invalidations == ["inspector"]


async def test_a_declared_output_schema_rejects_a_mismatched_return():
    schema = {
        "type": "object",
        "properties": {"tension": {"type": "integer"}},
        "required": ["tension"],
    }
    with pytest.raises(FlowError, match="does not match its declared output schema"):
        await run(flow({"op": "return", "value": {"tension": "high"}}), output_schema=schema)
