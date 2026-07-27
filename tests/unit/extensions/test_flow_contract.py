"""Flow language contract: values, predicates, structure, and context gating.

The properties under test are the ones that make a flow *bounded*: no forward
references, no unbounded nesting, no operation outside the hook stage that
allows it, and no way for a package string to become an expression.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.contracts import (
    Capability,
    EffectEnvelope,
    Flow,
    OpContext,
    check_context,
    declared_operations,
    derive_capabilities,
    iter_steps,
    validate_ref_path,
    validate_template,
)
from backend.features.extensions.limits import (
    MAX_FLOW_NESTING_DEPTH,
    MAX_PREDICATE_DEPTH,
)

SCORE_STEP = {
    "id": "score",
    "op": "model.structured",
    "lane": "agent",
    "prompt": {"$template": "Rate scene tension from 0 to 100.\n\n{{ctx.draft}}"},
    "output_schema": {
        "type": "object",
        "properties": {"tension": {"type": "integer", "minimum": 0, "maximum": 100}},
        "required": ["tension"],
        "additionalProperties": False,
    },
}


def flow(*steps) -> Flow:
    return Flow.model_validate({"flow_version": 1, "steps": list(steps)})


# ── the reference flow ───────────────────────────────────────────────────────


def test_reference_scene_meter_flow_validates():
    f = flow(
        SCORE_STEP,
        {
            "op": "state.set",
            "scope": "conversation",
            "path": "tension",
            "value": {"$ref": "steps.score.tension"},
        },
        {"op": "ui.invalidate", "view": "inspector"},
    )
    assert declared_operations(f) == {"model.structured", "state.set", "ui.invalidate"}
    assert derive_capabilities(f) == {
        Capability.MODEL_CALL,
        Capability.STATE_WRITE,
        Capability.UI_CONTRIBUTE,
    }


def test_unknown_flow_version_is_rejected():
    with pytest.raises(ValidationError):
        Flow.model_validate({"flow_version": 2, "steps": [{"op": "return"}]})


def test_unknown_operation_is_rejected():
    with pytest.raises(ValidationError):
        flow({"op": "os.system", "command": "rm -rf /"})


def test_unknown_step_field_is_rejected():
    with pytest.raises(ValidationError):
        flow({"op": "return", "eval": "1+1"})


def test_empty_flow_is_rejected():
    with pytest.raises(ValidationError):
        Flow.model_validate({"flow_version": 1, "steps": []})


def test_effect_envelope_has_a_typed_toast_channel():
    envelope = EffectEnvelope.model_validate(
        {
            "data": None,
            "effects": [],
            "toasts": [{"text": "Saved", "tone": "success"}],
            "runtime_generation": 3,
        }
    )
    assert envelope.toasts[0].text == "Saved"
    with pytest.raises(ValidationError):
        EffectEnvelope.model_validate(
            {
                "effects": [],
                "toasts": [{"text": "Nope", "tone": "javascript"}],
                "runtime_generation": 3,
            }
        )


# ── values ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "ctx.draft",
        "ctx.character.name",
        "input.message_id",
        "steps.score.tension",
        "host.conversation_id",
        "fragment.config.max_delta",
    ],
)
def test_valid_ref_paths(path):
    assert validate_ref_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "settings.api_key",
        "__import__",
        "ctx",
        "ctx.",
        "ctx..draft",
        "ctx.draft.__class__",
        "ctx.draft[0]",
        "a.b.c.d.e.f.g.h.i.j",
        123,
    ],
)
def test_invalid_ref_paths_are_rejected(path):
    with pytest.raises(ValueError):
        validate_ref_path(path)


def test_dunder_segments_are_rejected():
    # There is no attribute access in the resolver, but a path that *looks*
    # like one should not be expressible either -- it would be the first thing
    # tried against any future resolver change.
    with pytest.raises(ValueError):
        validate_ref_path("ctx.__dict__.x")


def test_ref_form_must_be_the_objects_only_key():
    with pytest.raises(ValidationError):
        flow({"op": "draft.replace", "value": {"$ref": "ctx.draft", "extra": 1}})


def test_reserved_dollar_prefix_is_not_usable_as_an_ordinary_key():
    with pytest.raises(ValidationError):
        flow({"op": "draft.replace", "value": {"$eval": "1+1"}})


def test_template_substitutes_paths_only():
    assert validate_template("a {{ctx.draft}} b") == "a {{ctx.draft}} b"


@pytest.mark.parametrize(
    "bad",
    [
        "{{ctx.draft | upper}}",
        "{{ctx.draft()}}",
        "{{settings.api_key}}",
        "{{ for x in y }}",
        "unbalanced {{",
        "unbalanced }}",
    ],
)
def test_templates_with_expression_syntax_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_template(bad)


def test_non_finite_numbers_cannot_appear_in_a_value():
    with pytest.raises(ValidationError):
        flow({"op": "math.negate", "value": float("nan")})


# ── predicates ───────────────────────────────────────────────────────────────


def test_predicate_ast_validates():
    f = flow(
        SCORE_STEP,
        {
            "op": "return",
            "when": {
                "and": [
                    {"exists": {"$ref": "ctx.draft"}},
                    {"gt": [{"$ref": "steps.score.tension"}, 3]},
                ]
            },
            "value": 1,
        },
    )
    assert len(list(iter_steps(f.steps))) == 2


def test_unknown_predicate_operator_is_rejected():
    with pytest.raises(ValidationError):
        flow({"op": "return", "when": {"matches": [{"$ref": "ctx.draft"}, ".*"]}})


def test_predicate_must_have_exactly_one_operator():
    with pytest.raises(ValidationError):
        flow({"op": "return", "when": {"eq": [1, 1], "ne": [1, 2]}})


def test_binary_predicate_arity_is_enforced():
    with pytest.raises(ValidationError):
        flow({"op": "return", "when": {"eq": [1, 2, 3]}})


def test_predicate_depth_is_bounded():
    node = {"exists": {"$ref": "ctx.draft"}}
    for _ in range(MAX_PREDICATE_DEPTH + 2):
        node = {"not": node}
    with pytest.raises(ValidationError):
        flow({"op": "return", "when": node})


# ── structure ────────────────────────────────────────────────────────────────


def test_forward_step_reference_is_rejected():
    with pytest.raises(ValidationError, match="before it runs"):
        flow(
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "t",
                "value": {"$ref": "steps.later.x"},
            },
            {**SCORE_STEP, "id": "later"},
        )


def test_reference_to_an_unknown_step_is_rejected():
    with pytest.raises(ValidationError, match="before it runs"):
        flow({"op": "draft.replace", "value": {"$ref": "steps.ghost.value"}})


def test_forward_reference_inside_a_template_is_rejected():
    with pytest.raises(ValidationError, match="before it runs"):
        flow(
            {"op": "ui.status", "text": {"$template": "at {{steps.later.x}}"}},
            {**SCORE_STEP, "id": "later"},
        )


def test_backward_reference_from_inside_a_branch_is_allowed():
    f = flow(
        SCORE_STEP,
        {
            "op": "if",
            "when": {"exists": {"$ref": "steps.score.tension"}},
            "then": [{"op": "ui.status", "text": {"$ref": "steps.score.tension"}}],
        },
    )
    assert len(list(iter_steps(f.steps))) == 3


def test_duplicate_step_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate step id"):
        flow({**SCORE_STEP, "id": "a"}, {**SCORE_STEP, "id": "a"})


def test_nesting_depth_is_bounded():
    step = {"op": "return", "value": 1}
    for _ in range(MAX_FLOW_NESTING_DEPTH + 2):
        step = {"op": "if", "when": {"eq": [1, 1]}, "then": [step]}
    with pytest.raises(ValidationError, match="nests deeper"):
        flow(step)


def test_on_error_continue_requires_a_fallback():
    with pytest.raises(ValidationError, match="fallback"):
        flow({**SCORE_STEP, "on_error": "continue"})


def test_fallback_without_continue_is_rejected():
    with pytest.raises(ValidationError, match="only meaningful"):
        flow({**SCORE_STEP, "fallback": {"tension": 0}})


def test_on_error_continue_with_fallback_is_accepted():
    f = flow({**SCORE_STEP, "on_error": "continue", "fallback": {"tension": 0}})
    assert f.steps[0].fallback == {"tension": 0}


def test_branch_activation_cannot_be_mixed_with_external_calls():
    # Branch activation holds the conversation stream lock and the pipeline
    # takes that lock before the workflow locks: a model call inside the window
    # is the deadlock shape.
    with pytest.raises(ValidationError, match="deadlock"):
        flow(
            SCORE_STEP,
            {
                "op": "conversation.branch.activate",
                "message_id": {"$ref": "input.message_id"},
            },
        )


def test_branch_activation_alone_is_accepted():
    f = flow(
        {
            "op": "conversation.branch.activate",
            "message_id": {"$ref": "input.message_id"},
        }
    )
    assert derive_capabilities(f) == {Capability.CONVERSATION_BRANCH_ACTIVATE}


# ── context gating ───────────────────────────────────────────────────────────


def test_draft_replace_is_transform_only():
    f = flow({"op": "draft.replace", "value": {"$ref": "ctx.draft"}})
    assert check_context(f, OpContext.POST_TRANSFORM) == []
    assert check_context(f, OpContext.POST_OBSERVE) != []
    assert check_context(f, OpContext.PRE_PIPELINE) != []
    assert check_context(f, OpContext.ACTION) != []


def test_context_append_is_pre_only():
    f = flow({"op": "context.append", "targets": ["writer"], "label": "Scene", "text": "x"})
    assert check_context(f, OpContext.PRE_PIPELINE) == []
    assert check_context(f, OpContext.POST_TRANSFORM) != []


def test_branch_activation_is_action_only():
    f = flow(
        {
            "op": "conversation.branch.activate",
            "message_id": {"$ref": "input.message_id"},
        }
    )
    assert check_context(f, OpContext.ACTION) == []
    assert check_context(f, OpContext.POST_OBSERVE) != []


@pytest.mark.parametrize(
    "step",
    [
        SCORE_STEP,
        {"op": "http.request", "method": "GET", "url": "https://x.invalid/a"},
        {"op": "state.set", "scope": "conversation", "path": "t", "value": 1},
        {"op": "state.get", "scope": "conversation", "path": "t"},
        {"op": "artifact.emit", "filename": "a.txt", "mime": "text/plain", "data": "x"},
        {"op": "ui.toast", "text": "hi"},
        {"op": "random.integer", "minimum": 0, "maximum": 9},
    ],
)
def test_reducer_profile_excludes_impure_operations(step):
    # A reducer is a pure function of (config, previous, director output). It
    # cannot call a model or network, touch state, emit UI or artifacts, mutate
    # the draft or context, or observe randomness.
    assert check_context(flow(step), OpContext.REDUCER) != []


def test_reducer_profile_allows_pure_arithmetic():
    f = flow(
        {
            "id": "sum",
            "op": "math.add",
            "a": {"$ref": "fragment.previous.value"},
            "b": {"$ref": "fragment.director.delta"},
        },
        {
            "id": "clamped",
            "op": "math.clamp",
            "value": {"$ref": "steps.sum"},
            "minimum": {"$ref": "fragment.config.minimum"},
            "maximum": {"$ref": "fragment.config.maximum"},
        },
        {"op": "return", "value": {"value": {"$ref": "steps.clamped"}}},
    )
    assert check_context(f, OpContext.REDUCER) == []
    assert derive_capabilities(f) == set()


def test_hidden_operation_behind_a_false_predicate_still_counts():
    # Conservative over all branches: a privileged operation gated by a
    # statically false predicate is still in the consent diff, because a
    # predicate's runtime value is not knowable at install time.
    f = flow(
        {
            "op": "if",
            "when": {"eq": [0, 1]},
            "then": [{"op": "http.request", "method": "GET", "url": "https://x.invalid/a"}],
        }
    )
    assert Capability.NETWORK_REQUEST in derive_capabilities(f)
    assert check_context(f, OpContext.REDUCER) != []


# ── schemas inside flows ─────────────────────────────────────────────────────


def test_structured_output_schema_must_be_in_the_subset():
    with pytest.raises(ValidationError):
        flow(
            {
                **SCORE_STEP,
                "output_schema": {
                    "type": "object",
                    "patternProperties": {".*": {"type": "string"}},
                },
            }
        )


def test_structured_output_schema_gets_closed_objects():
    f = flow(SCORE_STEP)
    assert f.steps[0].output_schema["additionalProperties"] is False
