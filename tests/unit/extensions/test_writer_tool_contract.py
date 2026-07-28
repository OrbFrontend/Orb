"""What an API 2 package may declare, and what a v1 package still may not.

The corpus is written the way a package that wants more than it was granted
would be written: asking the model for the draft, hiding a large prompt in
property descriptions, naming a host-supplied entity, reaching an operation the
Writer context excludes. Each of those is a compile-time refusal rather than a
runtime one, because the failure mode of the runtime version is a resolver that
breaks in front of a user mid-reply.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.compiler import compile_package
from backend.features.extensions.contracts import (
    CONTRIBUTION_MIN_API,
    ExtensionManifest,
    OpContext,
    check_context,
)
from backend.features.extensions.contracts.flow import Flow
from backend.features.extensions.errors import PackageValidationError
from backend.features.extensions.limits import (
    MAX_WRITER_TOOL_DESCRIPTION_CHARS,
    MAX_WRITER_TOOL_SCHEMA_BYTES,
)
from backend.features.extensions.sources import ArchiveSource
from tests.extension_packages import (
    orbext,
    outcome_resolver_manifest,
    outcome_resolver_package,
    resolve_outcome_flow,
)

TOOL = {
    "id": "resolve_outcome",
    "label": "Resolve outcome",
    "description": "Resolve an uncertain action.",
    "flow": "flows/resolve-outcome.json",
    "input_schema": {
        "type": "object",
        "properties": {"action": {"type": "string", "maxLength": 100}},
        "required": ["action"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"outcome": {"type": "string"}},
        "required": ["outcome"],
        "additionalProperties": False,
    },
}


def manifest(**tool_overrides) -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "extension_api": 2,
            "id": "outcome-resolver",
            "name": "Outcome Resolver",
            "version": "1.0.0",
            "permissions": [{"capability": "writer.tool.contribute"}],
            "contributions": {"writer_tool": {**TOOL, **tool_overrides}},
        }
    )


def compile_bytes(package: bytes):
    with ArchiveSource(package) as source:
        return compile_package(source)


# ── version contract ────────────────────────────────────────────────────────


def test_a_v1_manifest_may_not_declare_a_writer_tool():
    """v1 still means v1. Not "the field is ignored" -- a package cannot acquire
    semantics its author never declared by being parsed on a newer Orb."""
    with pytest.raises(ValidationError, match="requires extension_api 2"):
        ExtensionManifest.model_validate(
            {
                "extension_api": 1,
                "id": "outcome-resolver",
                "name": "Outcome Resolver",
                "version": "1.0.0",
                "permissions": [{"capability": "writer.tool.contribute"}],
                "contributions": {"writer_tool": TOOL},
            }
        )


def test_the_slot_table_names_the_version_that_introduced_each_contribution():
    assert CONTRIBUTION_MIN_API["writer_tool"] == 2


def test_a_v2_manifest_without_a_writer_tool_behaves_like_v1():
    parsed = ExtensionManifest.model_validate({"extension_api": 2, "id": "plain", "name": "Plain", "version": "1.0.0"})
    assert parsed.writer_tool is None
    assert parsed.writer_tool_wire_name() is None


# ── consent and naming ──────────────────────────────────────────────────────


def test_contributing_a_writer_tool_requires_its_own_grant():
    with pytest.raises(ValidationError, match="writer.tool.contribute"):
        ExtensionManifest.model_validate(
            {
                "extension_api": 2,
                "id": "outcome-resolver",
                "name": "Outcome Resolver",
                "version": "1.0.0",
                "contributions": {"writer_tool": TOOL},
            }
        )


def test_the_provider_name_is_derived_not_declared():
    assert manifest().writer_tool_wire_name() == "orb_writer_outcome-resolver--resolve_outcome"


def test_a_declared_provider_name_is_an_unknown_field():
    with pytest.raises(ValidationError):
        manifest(name="my_tool")


def test_an_overlong_id_pair_fails_at_parse_time():
    with pytest.raises(ValidationError, match="character limit"):
        ExtensionManifest.model_validate(
            {
                "extension_api": 2,
                "id": "a" * 40,
                "name": "Long",
                "version": "1.0.0",
                "permissions": [{"capability": "writer.tool.contribute"}],
                "contributions": {"writer_tool": {**TOOL, "id": "b" * 40}},
            }
        )


# ── schemas ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reserved", ["draft", "conversation_id", "card_id", "message_id", "history", "persona"])
def test_a_host_supplied_property_may_not_be_a_model_argument(reserved):
    """The host supplies the draft and every entity identity. A model argument
    that could name one would be an argument that redirects the invocation."""
    with pytest.raises(ValidationError, match="host-supplied"):
        manifest(
            input_schema={
                "type": "object",
                "properties": {reserved: {"type": "string"}},
                "required": [reserved],
                "additionalProperties": False,
            }
        )


def test_an_oversized_description_is_refused():
    """It is model input on every turn the tool is active, not catalog copy read
    once, so it is bounded far below ``MAX_DESCRIPTION_CHARS``."""
    with pytest.raises(ValidationError):
        manifest(description="x" * (MAX_WRITER_TOOL_DESCRIPTION_CHARS + 1))


def test_a_long_property_description_is_refused_too():
    """Otherwise the bound above is one refactor away from being cosmetic:
    splitting 20 KiB across forty properties puts the same text in the prompt."""
    with pytest.raises(ValidationError, match="description"):
        manifest(
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "y" * (MAX_WRITER_TOOL_DESCRIPTION_CHARS + 1),
                    }
                },
                "required": ["action"],
                "additionalProperties": False,
            }
        )


def test_a_non_object_input_schema_is_refused():
    with pytest.raises(ValidationError, match="must declare an object"):
        manifest(input_schema={"type": "string"})


def test_too_many_properties_are_refused():
    with pytest.raises(ValidationError, match="limit is"):
        manifest(
            input_schema={
                "type": "object",
                "properties": {f"p{i}": {"type": "string"} for i in range(20)},
                "required": [],
                "additionalProperties": False,
            }
        )


def test_an_over_budget_schema_fails_compilation():
    """The blob is prompt bytes the user did not author, so it is bounded where
    a declaration is bounded -- at compile time, not at the first turn."""
    package = orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(
                contributions={
                    "writer_tool": {
                        **TOOL,
                        "input_schema": {
                            "type": "object",
                            "properties": {f"p{i}": {"type": "string", "description": "z" * 500} for i in range(8)},
                            "required": [],
                            "additionalProperties": False,
                        },
                    }
                },
            ),
            "flows/resolve-outcome.json": resolve_outcome_flow(),
        }
    )
    with pytest.raises(PackageValidationError, match=str(MAX_WRITER_TOOL_SCHEMA_BYTES)):
        compile_bytes(package)


# ── compilation ─────────────────────────────────────────────────────────────


def test_the_compiled_spec_pins_the_revision_that_produced_it():
    compiled = compile_bytes(outcome_resolver_package())
    assert compiled.writer_tool is not None
    assert compiled.writer_tool.content_digest == compiled.digest
    assert compiled.writer_tool.schema["function"]["name"] == compiled.writer_tool.wire_name


def test_the_contribution_is_part_of_the_consent_fingerprint():
    """Its description ships to the model every turn, so changing it changes
    what the user consented to even though no capability name moved."""
    base = compile_bytes(outcome_resolver_package())
    reworded = compile_bytes(
        orbext(
            {
                "orb-extension.json": outcome_resolver_manifest(
                    contributions={
                        "writer_tool": {
                            **outcome_resolver_manifest()["contributions"]["writer_tool"],
                            "description": "Something entirely different.",
                        }
                    }
                ),
                "flows/resolve-outcome.json": resolve_outcome_flow(),
            }
        )
    )
    assert base.contract_fingerprint != reworded.contract_fingerprint
    assert sorted(base.requirements.permissions) == sorted(reworded.requirements.permissions)


def test_the_writer_flow_is_walked_and_its_grants_derived():
    compiled = compile_bytes(outcome_resolver_package())
    assert ("writer.tool.contribute", None) in compiled.requirements.permissions
    assert ("state.write", "conversation") in compiled.requirements.permissions
    assert ("context.read", "draft") in compiled.requirements.permissions


def test_an_undeclared_writer_flow_grant_fails_compilation():
    package = orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(
                permissions=[{"capability": "writer.tool.contribute"}],
                requires={"operations": ["state.set", "return"], "components": []},
            ),
            "flows/resolve-outcome.json": {
                "flow_version": 1,
                "steps": [
                    {"op": "state.set", "scope": "conversation", "path": "x", "value": 1},
                    {"op": "return", "value": {"outcome": "success", "roll": 1}},
                ],
            },
        }
    )
    with pytest.raises(PackageValidationError, match="does not request it"):
        compile_bytes(package)


# ── the operation profile ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "step",
    [
        {"op": "draft.replace", "value": "x"},
        {"op": "context.append", "targets": ["writer"], "label": "L", "text": "x"},
        {"op": "artifact.emit", "filename": "a.txt", "mime": "text/plain", "data": "x"},
        {"op": "card.tags.set", "tags": []},
        {"op": "conversation.branch.activate", "message_id": 1},
        {"op": "ui.toast", "text": "x"},
        {"op": "ui.invalidate", "view": "v"},
        {"op": "ui.status", "text": "x"},
    ],
)
def test_a_denied_operation_is_refused_in_the_writer_tool_context(step):
    flow = Flow.model_validate({"flow_version": 1, "steps": [step]})
    assert check_context(flow, OpContext.WRITER_TOOL)


@pytest.mark.parametrize(
    "step",
    [
        {"op": "state.get", "scope": "conversation", "path": "x"},
        {"op": "state.set", "scope": "character", "path": "x", "value": 1},
        {"op": "random.integer", "minimum": 1, "maximum": 6},
        {"op": "model.text", "lane": "agent", "prompt": "hi"},
        {"op": "http.request", "method": "GET", "url": "https://example.invalid/x"},
        {"op": "math.clamp", "value": 1, "minimum": 0, "maximum": 2},
        {"op": "return", "value": 1},
    ],
)
def test_an_allowed_operation_passes_the_writer_tool_context(step):
    flow = Flow.model_validate({"flow_version": 1, "steps": [step]})
    assert check_context(flow, OpContext.WRITER_TOOL) == []


def test_message_scoped_state_is_refused_at_compile_time():
    """There is no assistant row while the Writer is still writing it, so the
    target never exists -- and discovering that on the first live turn would
    mean a failed resolver in front of a user."""
    flow = Flow.model_validate({"flow_version": 1, "steps": [{"op": "state.set", "scope": "message", "path": "x", "value": 1}]})
    problems = check_context(flow, OpContext.WRITER_TOOL)
    assert any("scope 'message'" in problem for problem in problems)
    # Still legal where a message exists.
    assert check_context(flow, OpContext.POST_OBSERVE) == []
