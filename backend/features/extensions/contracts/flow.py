"""The flow language: a bounded sequence of host operations.

A flow has no user-defined functions, imports, recursion, cycles, or unbounded
iteration. Control flow is one construct (``if``) with an explicit early
``return``; everything else is a straight-line list of operations whose inputs
are validated ``$ref`` / ``$template`` values.

The three structural guarantees this module enforces, all at parse time:

* **No forward references.** ``steps.score`` resolves only to a step that has
  already run in program order. Without this, a package could describe a
  dependency the interpreter would have to satisfy by reordering or by
  iterating to a fixed point -- both of which are unbounded work.
* **Conservative context checking.** :func:`check_context` walks *every*
  branch, including ones guarded by a predicate that is statically false. An
  operation's presence is what matters for consent, not its reachability under
  some particular input.
* **Branch activation is never mixed with external effects.** Branch activation
  takes the conversation stream lock, which the pipeline acquires before the
  workflow locks; a model or HTTP call inside that window is exactly the
  ordering that deadlocks a turn against an action.

``on_error: "continue"`` requires an explicit ``fallback``. Otherwise a later
step could read the output of a step that did not produce one, and "undefined"
would become a value the language has to define.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, model_validator

from ..assets import ARTIFACT_MEDIA_TYPES, normalize_artifact_mime
from ..limits import MAX_FLOW_NESTING_DEPTH, MAX_FLOW_STEPS_DECLARED, MAX_JSON_MEMBERS
from .capabilities import (
    EXTERNAL_EFFECT_OPS,
    OPERATION_SPECS,
    Capability,
    OpContext,
)
from .common import (
    ExtModel,
    Label,
    LocalId,
    PackagePath,
    Predicate,
    SecretBearingValue,
    StepId,
    Value,
    template_paths,
)
from .schema_subset import parse_schema

StateScope = Literal["config", "conversation", "message", "character"]
Lane = Literal["writer", "agent"]
ContextTarget = Literal["director", "writer"]


def _validated_schema(raw: Any) -> dict:
    """Normalize a package-declared schema, keeping it a plain JSON dict.

    The model stores the normalized *dict* rather than a ``PackageSchema`` so a
    parsed flow re-encodes to canonical JSON without a custom serializer -- the
    stored manifest and the digest both depend on that round trip.
    """
    return dict(parse_schema(raw, what="output_schema").schema)


PackageSchemaDict = Annotated[dict, AfterValidator(_validated_schema)]


class _Step(ExtModel):
    """Fields every step shares.

    ``id`` is optional because only a step whose output is referenced needs a
    name; requiring one everywhere would make step ids part of the digest for
    no behavioral reason.
    """

    id: StepId | None = None
    when: Predicate | None = None
    on_error: Literal["stop", "continue"] = "stop"
    fallback: Value | None = None

    @model_validator(mode="after")
    def _fallback_required_for_continue(self):
        if self.on_error == "continue" and self.fallback is None:
            raise ValueError("a step with on_error='continue' must declare an explicit 'fallback' value")
        if self.on_error == "stop" and self.fallback is not None:
            raise ValueError("'fallback' is only meaningful with on_error='continue'")
        return self


class StateGetStep(_Step):
    op: Literal["state.get"]
    scope: StateScope
    path: LocalId


class StateSetStep(_Step):
    op: Literal["state.set"]
    scope: StateScope
    path: LocalId
    value: Value


class StateDeleteStep(_Step):
    op: Literal["state.delete"]
    scope: StateScope
    path: LocalId


class TextConcatStep(_Step):
    op: Literal["text.concat"]
    values: list[Value] = Field(min_length=1, max_length=MAX_JSON_MEMBERS)


class TextReplaceLiteralStep(_Step):
    op: Literal["text.replace_literal"]
    value: Value
    find: Value
    replace: Value


class JsonPickStep(_Step):
    op: Literal["json.pick"]
    value: Value
    keys: list[LocalId] = Field(min_length=1, max_length=MAX_JSON_MEMBERS)


class JsonMergeStep(_Step):
    op: Literal["json.merge"]
    values: list[Value] = Field(min_length=2, max_length=MAX_JSON_MEMBERS)


class ListIntersectStep(_Step):
    """Members of ``value`` that also appear in ``allowed``, in ``value`` order.

    It exists because a package-declared ``output_schema`` compiles at install
    time and therefore cannot carry an ``enum`` drawn from runtime config: an
    extension whose behavior is governed by a user-managed vocabulary has no
    other way to constrain a model result to that vocabulary. Without it, a
    classifier would launder invented values into first-party data under the
    user's own list.
    """

    op: Literal["list.intersect"]
    value: Value
    allowed: Value


class ListJoinStep(_Step):
    """Join an array of scalars with a separator from a closed host-owned set.

    The separator is an enumeration rather than a string because a free-form
    separator is one argument away from a format language, and the whole point
    of withdrawing the old scalar-array template rule was to keep templates at
    exactly one rule with no special cases.
    """

    op: Literal["list.join"]
    value: Value
    separator: Literal[", ", "; ", " ", "\n", "\n- "] = ", "


class MathAddStep(_Step):
    op: Literal["math.add"]
    a: Value
    b: Value


class MathSubtractStep(_Step):
    op: Literal["math.subtract"]
    a: Value
    b: Value


class MathNegateStep(_Step):
    op: Literal["math.negate"]
    value: Value


class MathClampStep(_Step):
    op: Literal["math.clamp"]
    value: Value
    minimum: Value
    maximum: Value


class RandomIntegerStep(_Step):
    op: Literal["random.integer"]
    minimum: int
    maximum: int

    @model_validator(mode="after")
    def _ordered(self):
        if self.minimum > self.maximum:
            raise ValueError("random.integer minimum exceeds maximum")
        return self


class RandomChoiceStep(_Step):
    op: Literal["random.choice"]
    values: list[Value] = Field(min_length=1, max_length=MAX_JSON_MEMBERS)


class IfStep(_Step):
    """The only control-flow construct: one predicate, two declared branches.

    ``when`` is optional on the base (every step may be guarded) but mandatory
    here, enforced by the validator rather than by re-declaring the field
    without a default -- an ``if`` with no condition is a typo for a block, and
    the language has no blocks.
    """

    op: Literal["if"]
    then: list[Step] = Field(default_factory=list, max_length=MAX_FLOW_STEPS_DECLARED)
    otherwise: list[Step] = Field(default_factory=list, alias="else", max_length=MAX_FLOW_STEPS_DECLARED)

    @model_validator(mode="after")
    def _requires_a_condition(self):
        if self.when is None:
            raise ValueError("an 'if' step must declare 'when'")
        if not self.then and not self.otherwise:
            raise ValueError("an 'if' step must declare at least one of 'then' or 'else'")
        return self


class ReturnStep(_Step):
    op: Literal["return"]
    value: Value | None = None


class ModelTextStep(_Step):
    op: Literal["model.text"]
    lane: Lane
    prompt: Value


class ModelStructuredStep(_Step):
    op: Literal["model.structured"]
    lane: Lane
    prompt: Value
    output_schema: PackageSchemaDict


class HttpRequestStep(_Step):
    op: Literal["http.request"]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    url: Value
    headers: dict[str, SecretBearingValue] = Field(default_factory=dict)
    body: SecretBearingValue | None = None
    response: Literal["json", "text", "bytes"] = "json"


class ContextAppendStep(_Step):
    op: Literal["context.append"]
    targets: list[ContextTarget] = Field(min_length=1, max_length=2)
    label: Label
    text: Value


class DraftReplaceStep(_Step):
    op: Literal["draft.replace"]
    value: Value


class ArtifactEmitStep(_Step):
    """Attach bytes the interpreter already holds to a message.

    The three sources the artifact contract names, and no fourth: ``data`` resolving to an
    opaque HTTP response handle, ``data`` resolving to text or JSON a prior step
    produced, or ``asset`` naming a validated package file. ``asset`` is a
    declared *path* rather than a value because the compiler has to know which
    files a package reaches -- an asset chosen at runtime could not be read,
    type-checked, digested, or served, since none of those happen at runtime.

    ``message_id`` is action-only and ``recovery`` is what a later regenerate or
    reroll gets handed back. Both are optional here and checked against the
    binding's context by the compiler, which knows which one a flow is bound to.
    """

    op: Literal["artifact.emit"]
    filename: Value
    mime: Annotated[str, Field(min_length=1, max_length=128)]
    data: Value | None = None
    asset: PackagePath | None = None
    annotation: Value | None = None
    recovery: Value | None = None
    message_id: Value | None = None

    @model_validator(mode="after")
    def _one_byte_source(self):
        if (self.data is None) == (self.asset is None):
            raise ValueError("artifact.emit must name exactly one of 'data' or 'asset'")
        if normalize_artifact_mime(self.mime) not in ARTIFACT_MEDIA_TYPES:
            raise ValueError(
                f"artifact.emit mime {self.mime!r} is not an inert media type; allowed: {sorted(ARTIFACT_MEDIA_TYPES)}"
            )
        return self


class CardTagsSetStep(_Step):
    """Replace the tag list of the card already in the invocation's context.

    There is deliberately no ``card_id`` field. The operation's blast radius is
    one card *by construction* rather than by quota: a flow cannot read card A
    and write card B, because the only card it can write is the one the host
    resolved into ``ctx.character``. ``extra="forbid"`` means a package that
    declares a card argument fails compilation rather than having the field
    quietly ignored.

    The host -- not the package -- normalizes the result through the same
    helper the character API uses, so the two produce byte-identical stored
    tags for the same input list.
    """

    op: Literal["card.tags.set"]
    tags: Value


class BranchActivateStep(_Step):
    op: Literal["conversation.branch.activate"]
    message_id: Value


class UiStatusStep(_Step):
    op: Literal["ui.status"]
    text: Value


class UiToastStep(_Step):
    op: Literal["ui.toast"]
    text: Value
    tone: Literal["info", "success", "warning", "error"] = "info"


class UiInvalidateStep(_Step):
    op: Literal["ui.invalidate"]
    view: LocalId


Step = Annotated[
    StateGetStep
    | StateSetStep
    | StateDeleteStep
    | TextConcatStep
    | TextReplaceLiteralStep
    | JsonPickStep
    | JsonMergeStep
    | ListIntersectStep
    | ListJoinStep
    | MathAddStep
    | MathSubtractStep
    | MathNegateStep
    | MathClampStep
    | RandomIntegerStep
    | RandomChoiceStep
    | IfStep
    | ReturnStep
    | ModelTextStep
    | ModelStructuredStep
    | HttpRequestStep
    | ContextAppendStep
    | DraftReplaceStep
    | ArtifactEmitStep
    | CardTagsSetStep
    | BranchActivateStep
    | UiStatusStep
    | UiToastStep
    | UiInvalidateStep,
    Field(discriminator="op"),
]

IfStep.model_rebuild()


class Flow(ExtModel):
    """One entry point's declarative body."""

    flow_version: Literal[1]
    steps: list[Step] = Field(min_length=1, max_length=MAX_FLOW_STEPS_DECLARED)

    @model_validator(mode="after")
    def _structural_rules(self):
        _assert_depth_and_count(self.steps)
        _assert_unique_ids(self.steps)
        _assert_no_forward_refs(self.steps)
        ops = {step.op for step in iter_steps(self.steps)}
        if "conversation.branch.activate" in ops and ops & EXTERNAL_EFFECT_OPS:
            offenders = sorted(ops & EXTERNAL_EFFECT_OPS)
            raise ValueError(
                f"a flow that activates a branch cannot also use {offenders}: branch activation holds the "
                f"conversation stream lock, and an external call inside it deadlocks against the pipeline"
            )
        return self


def iter_steps(steps: Sequence[Any]) -> Iterator[Any]:
    """Yield every step in program order, descending into ``if`` branches."""
    for step in steps:
        yield step
        if isinstance(step, IfStep):
            yield from iter_steps(step.then)
            yield from iter_steps(step.otherwise)


def _assert_depth_and_count(steps: Sequence[Any], depth: int = 1) -> int:
    if depth > MAX_FLOW_NESTING_DEPTH:
        raise ValueError(f"flow nests deeper than {MAX_FLOW_NESTING_DEPTH}")
    total = 0
    for step in steps:
        total += 1
        if isinstance(step, IfStep):
            total += _assert_depth_and_count(step.then, depth + 1)
            total += _assert_depth_and_count(step.otherwise, depth + 1)
    if depth == 1 and total > MAX_FLOW_STEPS_DECLARED:
        raise ValueError(f"flow declares {total} steps, limit is {MAX_FLOW_STEPS_DECLARED}")
    return total


def _assert_unique_ids(steps: Sequence[Any]) -> None:
    seen: set[str] = set()
    for step in iter_steps(steps):
        if step.id is None:
            continue
        if step.id in seen:
            raise ValueError(f"duplicate step id {step.id!r}")
        seen.add(step.id)


def _referenced_step_ids(value: Any) -> Iterator[str]:
    """Yield the step ids a value tree reads, from both ``$ref`` and templates."""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            path = value["$ref"]
            if isinstance(path, str) and path.startswith("steps."):
                yield path.split(".")[1]
            return
        if set(value) == {"$template"}:
            text = value["$template"]
            if isinstance(text, str):
                for hole in template_paths(text):
                    if hole.startswith("steps."):
                        yield hole.split(".")[1]
            return
        for item in value.values():
            yield from _referenced_step_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _referenced_step_ids(item)


_STRUCTURAL_FIELDS = frozenset({"id", "op", "on_error", "then", "otherwise"})


def _assert_no_forward_refs(steps: Sequence[Any]) -> None:
    """Reject a ``steps.X`` reference to a step that has not run yet.

    Program order is the traversal order of :func:`iter_steps`: a step inside an
    ``if`` branch may read anything declared before the ``if``, and a step after
    the ``if`` may read the branch's outputs (which resolve to the missing
    sentinel when that branch did not run -- a defined value, unlike a forward
    reference).
    """
    produced: set[str] = set()
    for step in iter_steps(steps):
        for field in type(step).model_fields:
            if field in _STRUCTURAL_FIELDS:
                continue
            for referenced in _referenced_step_ids(getattr(step, field)):
                if referenced not in produced:
                    raise ValueError(
                        f"step {step.id or step.op!r} references steps.{referenced} before it runs "
                        f"(forward and unknown step references are rejected)"
                    )
        if step.id is not None and OPERATION_SPECS[step.op].produces_output:
            produced.add(step.id)


def check_context(flow: Flow, context: OpContext) -> list[str]:
    """Return every reason *flow* may not run in *context*, or ``[]``.

    Conservative over all branches: an operation guarded by ``when`` still
    counts, because a hook allowlist that could be evaded by a predicate would
    not be an allowlist.
    """
    problems: list[str] = []
    for op in sorted({step.op for step in iter_steps(flow.steps)}):
        spec = OPERATION_SPECS[op]
        if context not in spec.contexts:
            problems.append(
                f"operation {op!r} is not allowed in a {context.value} flow (allowed: {sorted(c.value for c in spec.contexts)})"
            )
    problems.extend(_artifact_target_problems(flow, context))
    problems.extend(_no_message_row_scope_problems(flow, context))
    return problems


_NO_MESSAGE_ROW_CONTEXTS = frozenset({OpContext.WRITER_TOOL, OpContext.DETECTOR})
"""Contexts that run inside an unfinished turn, before the assistant row exists."""


def _no_message_row_scope_problems(flow: Flow, context: OpContext) -> Iterator[str]:
    """A flow running mid-turn cannot address message-scoped state.

    Not a runtime "there is no message in scope" -- a compile-time refusal. The
    assistant row does not exist while the Writer is mid-reply, nor while the
    Editor is auditing the draft it produced, so a package that writes message
    state in either context is describing a target that never exists for that
    entry point. Finding that out on the first live turn would mean a failed
    contribution in front of a user rather than a rejected install in front of
    the author.
    """
    if context not in _NO_MESSAGE_ROW_CONTEXTS:
        return
    for step in iter_steps(flow.steps):
        if step.op.startswith("state.") and step.scope == "message":
            yield (
                f"operation {step.op!r} cannot use scope 'message' in a {context.value} flow: the assistant "
                f"message does not exist yet while the turn is still producing it"
            )


def _artifact_target_problems(flow: Flow, context: OpContext) -> Iterator[str]:
    """Where an emitted artifact is allowed to say which message it lands on.

    A post hook's target is the assistant row being persisted -- it has no id
    yet, and naming one would let a hook attach to a message somewhere else in
    the conversation. A recovery flow's target is the attachment the framework
    is rebuilding, which it already knows. An action has neither binding, so it
    must name its target, and the interpreter proves that message belongs to the
    invocation's conversation before staging anything. All of it is checked at
    compile time so a package cannot be published in a shape whose failure mode
    is a half-executed flow.
    """
    for step in iter_steps(flow.steps):
        if step.op != "artifact.emit":
            continue
        if context is OpContext.ACTION and step.message_id is None:
            yield "artifact.emit in an action must name the 'message_id' it attaches to"
        if context is not OpContext.ACTION and step.message_id is not None:
            yield f"artifact.emit in a {context.value} flow attaches to the message being written and takes no 'message_id'"


def referenced_flow_assets(flow: Flow) -> set[str]:
    """Package asset paths a flow attaches, for the compiler's selected set.

    Assets reachable from a flow are as much a part of the revision as ones
    reachable from a view: they are read, type-checked, hashed into the digest,
    and served. Collecting them here rather than in the compiler keeps "what a
    flow reaches" a fact the flow module owns.
    """
    return {step.asset for step in iter_steps(flow.steps) if step.op == "artifact.emit" and step.asset is not None}


def derive_capabilities(flow: Flow) -> set[Capability]:
    """The capability set a flow's operations require, derived from the code.

    The manifest's declared ``permissions`` must cover this; an omission is a
    validation error, not a silently narrowed grant.
    """
    return {
        spec.capability for spec in (OPERATION_SPECS[step.op] for step in iter_steps(flow.steps)) if spec.capability is not None
    }


def declared_operations(flow: Flow) -> set[str]:
    """The operation names a flow uses, for the ``requires.operations`` check."""
    return {step.op for step in iter_steps(flow.steps)}
