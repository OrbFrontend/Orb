"""Snapshot-driven fragment resolution, budgets, and branch-state filtering."""

from __future__ import annotations

from types import MappingProxyType

from backend.inference import (
    build_direct_scene_tool,
    build_director_tool_prompt,
    build_style_injection,
)
from backend.pipeline.fragment_types import (
    reduce_fragment_outputs,
    resolve_fragment_instances,
    with_fragment_priors,
)
from backend.workflows.fragment_types import (
    BUILTIN_FRAGMENT_TYPES,
    MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET,
    FragmentReduceRequest,
    FragmentTypeDefinition,
    FragmentTypeInstance,
)
from backend.workflows.registry import RegistrySnapshot


def _snapshot(*, reducer_steps: int = 1, reducer_calls: list[str] | None = None) -> RegistrySnapshot:
    definition_ref: list[FragmentTypeDefinition] = []

    async def reduce(request: FragmentReduceRequest):
        if reducer_calls is not None:
            reducer_calls.append(str(request.fragment["id"]))
        for _ in range(reducer_steps):
            request.budget.charge_step()
        request.budget.charge(value=request.director)
        return request.director

    def instantiate(config):
        if config:
            raise ValueError("configuration must be empty")
        return FragmentTypeInstance(
            definition=definition_ref[0],
            config={},
            director_schema={"type": "string"},
            validate_director=lambda value: None if isinstance(value, str) else "must be a string",
            reduce_value=reduce,
            render_prior=lambda fragment, previous: f"{fragment['id']}={previous}",
            render_writer=lambda fragment, previous, current: f"{fragment['id']}: {current}",
            has_initial=True,
            initial_value="seed",
        )

    definition = FragmentTypeDefinition(
        type_id="example:counter",
        local_id="counter",
        label="Counter",
        description="test",
        storage="assistant_progressive",
        config_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        instantiate=instantiate,
        prompt_hint="test value",
        owner_id="example",
        content_digest="d" * 64,
    )
    definition_ref.append(definition)
    types = {**BUILTIN_FRAGMENT_TYPES, definition.type_id: definition}
    return RegistrySnapshot(
        generation=1,
        workflows=MappingProxyType({}),
        by_hook=MappingProxyType({}),
        digests=MappingProxyType({}),
        fragment_types=MappingProxyType(types),
    )


def _fragment(fid: str) -> dict:
    return {
        "id": fid,
        "label": fid,
        "description": fid,
        "field_type": "example:counter",
        "injection_label": fid,
        "type_config": {},
    }


def test_missing_namespaced_provider_is_diagnosed_and_never_coerced():
    fragment = {**_fragment("missing"), "field_type": "gone:counter"}
    result = resolve_fragment_instances(_snapshot(), [fragment])
    assert result.fragments == ()
    assert result.inert_fragment_ids == ("missing",)
    assert result.diagnostics[0]["field_type"] == "gone:counter"
    assert "unavailable" in result.diagnostics[0]["message"]


def test_extension_instance_limit_is_deterministic():
    result = resolve_fragment_instances(_snapshot(), [_fragment(f"f{i:02}") for i in range(52)])
    assert [fragment["id"] for fragment in result.fragments] == [f"f{i:02}" for i in range(50)]
    assert [item["fragment_id"] for item in result.diagnostics] == ["f50", "f51"]
    assert result.inert_fragment_ids == ("f50", "f51")


async def test_shared_reducer_budget_fails_one_instance_without_losing_its_prior():
    calls: list[str] = []
    resolved = resolve_fragment_instances(
        _snapshot(reducer_steps=300, reducer_calls=calls),
        [_fragment("a"), _fragment("b"), _fragment("c")],
    ).fragments
    prepared, _ = with_fragment_priors(resolved, {})
    result = await reduce_fragment_outputs(
        prepared,
        {"a": "advanced-a", "b": "advanced-b", "c": "advanced-c"},
        is_cancelled=lambda: False,
    )
    assert result.progressive_fields == {"a": "advanced-a", "b": "seed", "c": "seed"}
    assert calls == ["a", "b"]
    assert result.diagnostics == (
        {
            "fragment_id": "b",
            "field_type": "example:counter",
            "message": "the fragment reducer failed; the prior value was retained",
        },
        {
            "fragment_id": "c",
            "field_type": "example:counter",
            "message": "the fragment reducer failed; the prior value was retained",
        },
    )


async def test_unresolved_progressive_value_is_carried_without_rendering_a_fragment():
    carried = {"missing": {"value": 42}}
    result = await reduce_fragment_outputs(
        (),
        {},
        is_cancelled=lambda: False,
        carried_progressive=carried,
    )
    assert result.fields == {}
    assert result.rendered_fragments == ()
    assert result.progressive_fields == carried


async def test_fragment_context_has_an_aggregate_budget_per_prompt_target():
    first_value = "a" * (MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET // 2)
    second_value = "b" * (MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET // 2)
    resolved = resolve_fragment_instances(_snapshot(), [_fragment("a"), _fragment("b")]).fragments
    prepared, prior_diagnostics = with_fragment_priors(
        resolved,
        {"a": first_value, "b": second_value},
    )
    assert prepared[0]["prior_context"]
    assert prepared[1]["prior_context"] == ""
    assert "aggregate Director" in prior_diagnostics[0]["message"]
    director_prompt = build_director_tool_prompt(
        "direct_scene",
        "continue",
        [],
        [],
        interactive_fragments=prepared,
        progressive_state={"a": first_value, "b": second_value},
        tool_schema=build_direct_scene_tool(prepared),
    )
    assert first_value in director_prompt
    assert second_value not in director_prompt

    reduced = await reduce_fragment_outputs(
        prepared,
        {"a": first_value, "b": second_value},
        is_cancelled=lambda: False,
    )
    assert reduced.rendered_fragments[0]["writer_context"]
    assert reduced.rendered_fragments[1]["writer_context"] == ""
    assert any("aggregate Writer" in item["message"] for item in reduced.diagnostics)
    writer_prompt = build_style_injection(
        [],
        interactive_fragments=reduced.rendered_fragments,
        extra_fields=reduced.fields,
    )
    assert first_value in writer_prompt
    assert second_value not in writer_prompt
