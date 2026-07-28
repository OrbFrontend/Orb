"""The flow interpreter: bounded execution with a staged effect transaction.

One invocation is a prepare/commit split. This module owns the middle of it --
executing steps under quotas and cancellation while *staging* every Orb-owned
effect -- and returns those effects to the caller, which owns acquiring locks
before and committing under them after. Nothing here writes to the database,
publishes an event, or touches the registry.

The shape that makes partial mutation impossible:

* **Effects are values until the flow returns.** ``state.set``,
  ``context.append``, ``draft.replace``, ``ui.toast``, and ``ui.invalidate``
  append to a :class:`StagedEffects` record. A flow that fails at step 9 leaves
  the eight prior steps' effects in that record, and the caller throws it away.
  There is no operation that applies where it appears.
* **Model calls are the exception, and are known to be.** A completion cannot
  be rolled back. It still cannot make a partial Orb mutation visible, because
  the mutation is not applied until the flow returns.
* **Grants are checked twice.** The compiler proved the flow's requirement set
  is covered before the entry point was published; every privileged operation
  re-checks the *live* grant view immediately before it runs. Revocation
  therefore stops the next operation of an already-running flow, which is the
  one place snapshot isolation is deliberately given up.

Errors are sanitized by construction: a message names an operation, a step id,
a limit, or a schema location. It never carries a resolved value, a model
response, or package content -- an action returns this string to the user, and
a hook writes it to a log.

Execution yields ``{"type": "status", ...}`` progress dicts and finishes with
exactly one :class:`FlowResult`, the same drain-to-terminal shape the pipeline
uses for ``_TurnSetup`` and ``_PostPipelineResult``.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
import math
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ...core import normalize_tags
from ...inference import IsolatedCallError, isolated_structured, isolated_text
from .contracts import (
    OPERATION_SPECS,
    Capability,
    Flow,
    OpContext,
    PackageSchema,
    Quota,
    declared_operations,
)
from .digest import canonical_json_bytes
from .errors import FlowCancelled, FlowError
from .limits import (
    MAX_ACTION_RESULT_BYTES,
    MAX_CARD_TAG_WRITES_PER_INVOCATION,
    MAX_CONTEXT_BLOCK_BYTES,
    MAX_DRAFT_BYTES,
    MAX_FLOW_STEPS_EXECUTED,
    MAX_LIST_OPERATION_MEMBERS,
    MAX_MODEL_CALLS_PER_INVOCATION,
    MAX_MODEL_OUTPUT_BYTES,
    MAX_MODEL_OUTPUT_TOKENS,
    MAX_STATE_BYTES_PER_SCOPE,
)
from .values import (
    MISSING,
    assert_concrete,
    assert_json_bounds,
    evaluate_predicate,
    resolve_path,
    resolve_value,
    scalar_text,
    values_equal,
)

logger = logging.getLogger(__name__)

DELETED = object()
"""Marker for a staged ``state.delete``, distinct from a stored JSON ``null``."""

MAX_STATUS_CHARS = 200
"""Cap on one ``ui.status`` line. Progress text, not a channel."""

MAX_VALUE_BYTES = MAX_DRAFT_BYTES
"""Cap on any intermediate string a pure operation produces.

Equal to the largest thing a flow is allowed to *finish* with, so an
intermediate value can never be the step that smuggles past a final quota:
concatenating two values that each pass this bound still has to pass it again.
"""

UNIMPLEMENTED_OPS: frozenset[str] = frozenset({"http.request", "artifact.emit"})
"""Operations this build parses and consents to, but cannot yet execute.

Network and artifact bytes arrive with the pinned-address HTTP client and the
artifact recovery contract. Until then an entry point that reaches one of them
is *blocked* with a diagnostic rather than published and failed halfway through
-- the same treatment an under-granted entry point gets. The interpreter also
refuses them defensively, so a publishing bug cannot turn into a half-executed
flow.
"""


# ── staged effects ───────────────────────────────────────────────────────────


@dataclass(slots=True)
class StagedEffects:
    """Everything one invocation wants to change, held until it succeeds.

    ``state`` folds multiple writes to the same path in program order, so the
    final slot-size check sees what would actually be stored rather than the
    sum of every intermediate write.
    """

    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    context_blocks: list[dict[str, Any]] = field(default_factory=list)
    draft: str | None = None
    card_tags: tuple[str, list[str]] | None = None
    """``(card_id, normalized tags)`` staged by ``card.tags.set``.

    A tuple rather than a list of writes because a flow may stage at most one:
    the card is fixed by the invocation's context, so a second write would
    either be to the same card (making the first dead) or to a card the flow
    was never handed."""
    branch_activation: int | None = None
    """The message id whose branch this invocation wants active. At most one:
    two activations in one flow would make the final active branch depend on
    step order rather than on anything the user chose."""
    toasts: list[dict[str, str]] = field(default_factory=list)
    invalidations: list[str] = field(default_factory=list)

    def slot(self, scope: str) -> dict[str, Any]:
        return self.state.setdefault(scope, {})


@dataclass(slots=True)
class FlowResult:
    """The terminal value of a successful invocation."""

    value: Any
    effects: StagedEffects


# ── host services ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ModelLane:
    """One resolved lane a ``model.*`` operation may call.

    Carries the client and model name only. Not a ``CachedBase``, not a prefix,
    not a tool blob: an extension call inherits none of the pipeline's shared
    bytes, and there is no field here through which it could.
    """

    client: Any
    model: str


@dataclass(frozen=True, slots=True)
class HostServices:
    """The whole surface the interpreter may reach outside itself.

    Passing these in rather than importing them is what keeps the interpreter
    testable without a database and, more usefully, makes the reachable set
    reviewable: if a capability is not a field here, no operation can perform
    it, whatever a package declares.

    ``grants`` is a *callable*, not a set. It is invoked immediately before each
    privileged operation so a revocation that lands mid-flow stops the next one;
    a captured set would let an already-running flow finish using authority the
    user has withdrawn.
    """

    grants: Callable[[], frozenset[tuple[str, str | None]]]
    read_state: Callable[[str], Awaitable[Mapping[str, Any]]]
    lanes: Mapping[str, ModelLane] = field(default_factory=dict)
    is_cancelled: Callable[[], bool] = lambda: False
    context_block_error: Callable[[list[dict[str, Any]]], str | None] | None = None
    owns_message: Callable[[int], Awaitable[bool]] | None = None
    """Whether a message id belongs to this invocation's conversation.

    Branch activation checks this while it *stages*, not only when it commits,
    so selecting a foreign message fails the flow before any other effect is
    written. The commit path re-checks atomically anyway; this is the check
    that makes the failure clean rather than partial."""


@dataclass(slots=True)
class Invocation:
    """One flow execution's identity, inputs, budget, and staging record."""

    extension_id: str
    context: OpContext
    host: HostServices
    ctx: Mapping[str, Any] = field(default_factory=dict)
    action_input: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    fragment: Mapping[str, Any] = field(default_factory=dict)
    seed: str = ""
    scopes_in_scope: frozenset[str] = frozenset()
    output_schema: PackageSchema | None = None

    effects: StagedEffects = field(default_factory=StagedEffects)
    steps_executed: int = 0
    quotas: dict[str, int] = field(default_factory=dict)
    _rng: random.Random | None = None
    state_snapshots: dict[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def rng(self) -> random.Random:
        """The per-invocation PRNG, seeded from host invocation identity.

        Never module-global randomness: two hooks in the same turn must not
        perturb each other's draws, and a regenerate that reuses the recorded
        seed must replay the same numbers. Derived through SHA-256 so the seed
        material can be readable strings without their concatenation collapsing
        two different invocations onto one stream.
        """
        if self._rng is None:
            digest = hashlib.sha256(f"orb-ext-v1|{self.extension_id}|{self.seed}".encode()).digest()
            self._rng = random.Random(int.from_bytes(digest[:16], "big"))
        return self._rng

    def charge(self, quota: Quota, limit: int, label: str) -> None:
        used = self.quotas.get(quota.value, 0) + 1
        if used > limit:
            raise FlowError(f"flow exceeded its budget of {limit} {label}")
        self.quotas[quota.value] = used

    def require(self, capability: Capability, parameter: str | None = None) -> None:
        """Check the live grant view, or refuse the operation."""
        granted = self.host.grants()
        if (capability.value, parameter) in granted:
            return
        detail = capability.value if parameter is None else f"{capability.value} for {parameter!r}"
        raise FlowError(f"permission {detail} is not granted")


# ── execution ────────────────────────────────────────────────────────────────


async def run_flow(flow: Flow, invocation: Invocation) -> AsyncIterator[dict | FlowResult]:
    """Execute *flow*, yielding progress dicts and one terminal :class:`FlowResult`.

    Raises :class:`FlowError` on any failure -- the caller discards
    ``invocation.effects`` and nothing reaches Orb. A successful return means
    every staged effect passed its own validation *and* the flow chose to
    finish, which is the two-part condition the commit step relies on.
    """
    namespaces: dict[str, Any] = {
        "ctx": invocation.ctx,
        "input": invocation.action_input,
        "host": invocation.metadata,
        "steps": {},
        "fragment": invocation.fragment,
    }
    returned = False
    value: Any = None
    async for event in _run_steps(flow.steps, invocation, namespaces):
        if isinstance(event, _Return):
            returned, value = True, event.value
            break
        yield event
    if not returned:
        value = None
    yield FlowResult(value=_validate_output(value, invocation), effects=invocation.effects)


@dataclass(slots=True)
class _Return:
    """Internal terminal marker for an executed ``return`` step."""

    value: Any


def _validate_output(value: Any, invocation: Invocation) -> Any:
    """Bound and schema-check a flow's return value.

    Falling off the end returns JSON ``null``; an explicit ``return`` is checked
    against the entry point's declared output schema when it has one. The size
    bound applies either way, because "the schema allows a string" is not a
    statement about how long that string may be.
    """
    if value is MISSING:
        raise FlowError("a flow cannot return a value that resolved to nothing")
    assert_json_bounds(value, what="the flow's return value")
    _assert_encoded_size(value, MAX_ACTION_RESULT_BYTES, "the flow's return value")
    if invocation.output_schema is not None:
        reason = invocation.output_schema.validate(value)
        if reason is not None:
            raise FlowError(f"the flow's return value does not match its declared output schema: {reason}")
    return value


async def _run_steps(
    steps: Sequence[Any],
    invocation: Invocation,
    namespaces: dict[str, Any],
) -> AsyncIterator[dict | _Return]:
    """Run one straight-line block, descending into a taken ``if`` branch."""
    for step in steps:
        if invocation.host.is_cancelled():
            raise FlowCancelled("the invocation was cancelled")
        invocation.steps_executed += 1
        if invocation.steps_executed > MAX_FLOW_STEPS_EXECUTED:
            raise FlowError(f"flow executed more than {MAX_FLOW_STEPS_EXECUTED} steps")

        # ``IfStep.when`` selects its branch. Every other step uses ``when`` as
        # an optional guard, so applying the generic guard to ``if`` would skip
        # the construct on false and make its ``else`` branch unreachable.
        if step.op != "if" and step.when is not None and not evaluate_predicate(step.when, namespaces):
            continue

        try:
            async for event in _run_step(step, invocation, namespaces):
                yield event
        except FlowCancelled:
            raise
        except FlowError as exc:
            if step.on_error != "continue":
                raise FlowError(f"step {_label(step)}: {exc}") from None
            # ``continue`` requires a declared fallback, so the later steps that
            # read this one's output always find a defined value -- which is why
            # "undefined" is not a state the language has to define.
            logger.info(
                "extension %r step %s failed, using fallback: %s",
                invocation.extension_id,
                _label(step),
                exc,
            )
            if step.id is not None:
                namespaces["steps"][step.id] = resolve_value(step.fallback, namespaces)


def _label(step: Any) -> str:
    return f"{step.id!r} ({step.op})" if step.id else f"{step.op!r}"


async def _run_step(step: Any, inv: Invocation, ns: dict[str, Any]) -> AsyncIterator[dict | _Return]:
    """Dispatch one operation, recording its output under the step id."""
    op = step.op
    if op in UNIMPLEMENTED_OPS:
        raise FlowError(f"operation {op!r} is not available in this Orb build")
    spec = OPERATION_SPECS[op]
    if inv.context not in spec.contexts:
        # Unreachable through a compiled package: the compiler checks contexts
        # per binding. Kept because the interpreter is the last gate before an
        # effect is staged, and a hook allowlist enforced only at compile time
        # would be one publishing bug away from not being an allowlist.
        raise FlowError(f"operation {op!r} is not allowed in a {inv.context.value} flow")

    if op == "if":
        branch = step.then if evaluate_predicate(step.when, ns) else step.otherwise
        async for event in _run_steps(branch, inv, ns):
            yield event
        return
    if op == "return":
        returned = None
        if step.value is not None:
            returned = assert_concrete(resolve_value(step.value, ns), what="the returned value")
        yield _Return(returned)
        return
    if op == "ui.status":
        yield {
            "type": "status",
            "text": _text(step.text, ns, "ui.status text")[:MAX_STATUS_CHARS],
        }
        return

    output = _HANDLERS[op](step, inv, ns)
    if inspect.isawaitable(output):
        output = await output
    if step.id is not None and spec.produces_output:
        ns["steps"][step.id] = output


# ── state ────────────────────────────────────────────────────────────────────


async def _state_slot(inv: Invocation, scope: str) -> Mapping[str, Any]:
    """This extension's slot for *scope*, read once per invocation.

    Once, not per operation: the caller holds the scope's lock across the whole
    invocation, so a second read could only return the same bytes -- and if it
    ever returned different ones, the read-modify-write the lock exists to
    protect would already have been broken.
    """
    if scope not in inv.state_snapshots:
        if scope not in inv.scopes_in_scope:
            raise FlowError(f"there is no {scope} in scope for this invocation")
        inv.state_snapshots[scope] = await inv.host.read_state(scope)
    return inv.state_snapshots[scope]


async def _op_state_get(step: Any, inv: Invocation, ns: dict[str, Any]) -> Any:
    inv.require(Capability.STATE_READ, step.scope)
    stored = await _state_slot(inv, step.scope)
    staged = inv.effects.state.get(step.scope, {})
    if step.path in staged:
        value = staged[step.path]
        return MISSING if value is DELETED else value
    return stored.get(step.path, MISSING)


async def _op_state_set(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    inv.require(Capability.STATE_WRITE, step.scope)
    inv.charge(Quota.STATE_WRITE, MAX_FLOW_STEPS_EXECUTED, "state writes")
    await _state_slot(inv, step.scope)  # proves the scope exists before staging
    value = assert_json_bounds(
        assert_concrete(resolve_value(step.value, ns), what=f"the value written to {step.path!r}"),
        what=f"the value written to {step.path!r}",
    )
    slot = inv.effects.slot(step.scope)
    slot[step.path] = value
    _assert_slot_size(inv, step.scope, slot)


async def _op_state_delete(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    inv.require(Capability.STATE_WRITE, step.scope)
    inv.charge(Quota.STATE_WRITE, MAX_FLOW_STEPS_EXECUTED, "state writes")
    await _state_slot(inv, step.scope)
    inv.effects.slot(step.scope)[step.path] = DELETED


def _assert_slot_size(inv: Invocation, scope: str, staged: Mapping[str, Any]) -> None:
    """Check the *resulting* slot against the per-scope cap, not the delta.

    The cap is on what would be stored, so it is measured against the merge of
    the stored slot and the staged writes. Checking only the new value would let
    a flow fill a slot one small write at a time.
    """
    merged = dict(inv.state_snapshots.get(scope) or {})
    for path, value in staged.items():
        if value is DELETED:
            merged.pop(path, None)
        else:
            merged[path] = value
    _assert_encoded_size(merged, MAX_STATE_BYTES_PER_SCOPE, f"the extension's {scope} state")


def _assert_encoded_size(value: Any, limit: int, what: str) -> None:

    try:
        size = len(canonical_json_bytes(value))
    except (TypeError, ValueError):
        raise FlowError(f"{what} is not encodable as JSON") from None
    if size > limit:
        raise FlowError(f"{what} is {size} bytes, over the {limit} byte limit")


# ── pure transforms ──────────────────────────────────────────────────────────


def _text(value: Any, ns: dict[str, Any], what: str) -> str:
    resolved = resolve_value(value, ns)
    if resolved is MISSING:
        raise FlowError(f"{what} resolved to nothing")
    text = scalar_text(resolved, what=what)
    if len(text.encode("utf-8")) > MAX_VALUE_BYTES:
        raise FlowError(f"{what} is over the {MAX_VALUE_BYTES} byte value limit")
    return text


def _number(value: Any, ns: dict[str, Any], what: str) -> float | int:
    resolved = resolve_value(value, ns)
    if isinstance(resolved, bool) or not isinstance(resolved, (int, float)):
        raise FlowError(f"{what} is not a number")
    return resolved


def _op_text_concat(step: Any, inv: Invocation, ns: dict[str, Any]) -> str:
    parts = [_text(item, ns, "text.concat operand") for item in step.values]
    joined = "".join(parts)
    if len(joined.encode("utf-8")) > MAX_VALUE_BYTES:
        raise FlowError(f"text.concat result is over the {MAX_VALUE_BYTES} byte value limit")
    return joined


def _op_text_replace(step: Any, inv: Invocation, ns: dict[str, Any]) -> str:
    subject = _text(step.value, ns, "text.replace_literal value")
    find = _text(step.find, ns, "text.replace_literal find")
    if not find:
        raise FlowError("text.replace_literal cannot search for an empty string")
    replaced = subject.replace(find, _text(step.replace, ns, "text.replace_literal replace"))
    if len(replaced.encode("utf-8")) > MAX_VALUE_BYTES:
        raise FlowError(f"text.replace_literal result is over the {MAX_VALUE_BYTES} byte value limit")
    return replaced


def _op_json_pick(step: Any, inv: Invocation, ns: dict[str, Any]) -> dict:
    source = resolve_value(step.value, ns)
    if not isinstance(source, dict):
        raise FlowError("json.pick needs an object")
    return {key: source[key] for key in step.keys if key in source}


def _op_json_merge(step: Any, inv: Invocation, ns: dict[str, Any]) -> dict:
    merged: dict[str, Any] = {}
    for item in step.values:
        resolved = resolve_value(item, ns)
        if not isinstance(resolved, dict):
            raise FlowError("json.merge needs objects")
        merged.update(resolved)
    return assert_json_bounds(merged, what="the json.merge result")


def _scalar_list(value: Any, ns: dict[str, Any], what: str) -> list[Any]:
    """Resolve a value into a bounded array of JSON scalars.

    Both list operations take arrays that a *model* or a user's config produced,
    so the shape check is a runtime one. Rejecting a container member rather
    than stringifying it keeps ``list.join`` from being the place ``[object]``
    enters a prompt.
    """
    resolved = resolve_value(value, ns)
    if not isinstance(resolved, list):
        raise FlowError(f"{what} is not an array")
    if len(resolved) > MAX_LIST_OPERATION_MEMBERS:
        raise FlowError(f"{what} has more than {MAX_LIST_OPERATION_MEMBERS} members")
    for item in resolved:
        if item is MISSING or isinstance(item, (dict, list)):
            raise FlowError(f"{what} must contain only scalars")
    return resolved


def _op_list_intersect(step: Any, inv: Invocation, ns: dict[str, Any]) -> list[Any]:
    """Members of ``value`` that also appear in ``allowed``, deduplicated.

    Order comes from ``value`` so the result reads as "what the model chose,
    minus what it invented" rather than as a re-sorted vocabulary. Membership
    uses the same type-strict equality predicates use, so ``1`` never matches
    ``True`` and ``"1"`` never matches ``1``.
    """
    values = _scalar_list(step.value, ns, "list.intersect value")
    allowed = _scalar_list(step.allowed, ns, "list.intersect allowed")
    result: list[Any] = []
    for item in values:
        if any(values_equal(item, candidate) for candidate in allowed) and not any(values_equal(item, kept) for kept in result):
            result.append(item)
    return result


def _op_list_join(step: Any, inv: Invocation, ns: dict[str, Any]) -> str:
    items = _scalar_list(step.value, ns, "list.join value")
    joined = step.separator.join(scalar_text(item, what="list.join member") for item in items)
    if len(joined.encode("utf-8")) > MAX_VALUE_BYTES:
        raise FlowError(f"list.join result is over the {MAX_VALUE_BYTES} byte value limit")
    return joined


def _op_math_add(step: Any, inv: Invocation, ns: dict[str, Any]) -> float | int:
    return _finite(_number(step.a, ns, "math.add a") + _number(step.b, ns, "math.add b"))


def _op_math_subtract(step: Any, inv: Invocation, ns: dict[str, Any]) -> float | int:
    return _finite(_number(step.a, ns, "math.subtract a") - _number(step.b, ns, "math.subtract b"))


def _op_math_negate(step: Any, inv: Invocation, ns: dict[str, Any]) -> float | int:
    return _finite(-_number(step.value, ns, "math.negate value"))


def _op_math_clamp(step: Any, inv: Invocation, ns: dict[str, Any]) -> float | int:
    value = _number(step.value, ns, "math.clamp value")
    low = _number(step.minimum, ns, "math.clamp minimum")
    high = _number(step.maximum, ns, "math.clamp maximum")
    if low > high:
        raise FlowError("math.clamp minimum exceeds its maximum")
    return _finite(min(max(value, low), high))


def _finite(value: float | int) -> float | int:
    if isinstance(value, float) and not math.isfinite(value):
        raise FlowError("arithmetic produced a non-finite number")
    return value


def _op_random_integer(step: Any, inv: Invocation, ns: dict[str, Any]) -> int:
    return inv.rng.randint(step.minimum, step.maximum)


def _op_random_choice(step: Any, inv: Invocation, ns: dict[str, Any]) -> Any:
    return resolve_value(step.values[inv.rng.randrange(len(step.values))], ns)


# ── host capabilities ────────────────────────────────────────────────────────


def _lane(inv: Invocation, name: str) -> ModelLane:
    inv.require(Capability.MODEL_CALL, name)
    lane = inv.host.lanes.get(name)
    if lane is None:
        raise FlowError(f"the {name} model lane is not configured")
    return lane


async def _op_model_text(step: Any, inv: Invocation, ns: dict[str, Any]) -> str:
    lane = _lane(inv, step.lane)
    inv.charge(Quota.MODEL_CALL, MAX_MODEL_CALLS_PER_INVOCATION, "model calls")
    try:
        value = await isolated_text(
            lane.client,
            model=lane.model,
            prompt=_text(step.prompt, ns, "model.text prompt"),
            max_tokens=MAX_MODEL_OUTPUT_TOKENS,
            max_output_bytes=MAX_MODEL_OUTPUT_BYTES,
        )
        return assert_json_bounds(value, what="the model's text output")
    except IsolatedCallError as exc:
        raise FlowError(str(exc)) from None


async def _op_model_structured(step: Any, inv: Invocation, ns: dict[str, Any]) -> Any:
    lane = _lane(inv, step.lane)
    inv.charge(Quota.MODEL_CALL, MAX_MODEL_CALLS_PER_INVOCATION, "model calls")
    try:
        value = await isolated_structured(
            lane.client,
            model=lane.model,
            prompt=_text(step.prompt, ns, "model.structured prompt"),
            schema=step.output_schema,
            max_tokens=MAX_MODEL_OUTPUT_TOKENS,
            max_output_bytes=MAX_MODEL_OUTPUT_BYTES,
        )
    except IsolatedCallError as exc:
        raise FlowError(str(exc)) from None
    assert_json_bounds(value, what="the model's structured output")
    # Validated against the compiled local schema, not merely requested through
    # it: a provider that ignores the constraint must fail the step rather than
    # hand a package's later steps a shape they were written to trust. The
    # stored dict is already the normalized schema the contract parser produced,
    # so this wraps rather than re-parses it.
    reason = PackageSchema(schema=step.output_schema).validate(value)
    if reason is not None:
        raise FlowError(f"the model's structured output does not match the declared schema: {reason}")
    return value


def _op_context_append(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    for target in step.targets:
        inv.require(Capability.PROMPT_CONTEXT_APPEND, target)
    inv.charge(Quota.CONTEXT_BLOCK, MAX_FLOW_STEPS_EXECUTED, "context blocks")
    text = _text(step.text, ns, "context.append text")
    size = len(text.encode("utf-8"))
    if size > MAX_CONTEXT_BLOCK_BYTES:
        raise FlowError(f"context block is {size} bytes, over the {MAX_CONTEXT_BLOCK_BYTES} byte limit")
    inv.effects.context_blocks.append({"targets": list(step.targets), "label": step.label, "text": text})


def _op_draft_replace(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    inv.require(Capability.DRAFT_REPLACE)
    inv.charge(Quota.DRAFT_REPLACE, 1, "draft replacements")
    draft = _text(step.value, ns, "draft.replace value")
    if len(draft.encode("utf-8")) > MAX_DRAFT_BYTES:
        raise FlowError(f"the replacement draft is over the {MAX_DRAFT_BYTES} byte limit")
    inv.effects.draft = draft


def _op_card_tags_set(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    """Stage a replacement tag list for the card in the invocation's context.

    Both grants are checked, not just the write: ``card.write`` scoped to
    ``tags`` alone would be a permission whose target the package cannot see,
    and the consent line ("the character a command is run against") would
    describe a scope the grant did not actually have.

    The tags are normalized here by the *host* normalizer -- the same one the
    character API runs -- so what a flow stages is already what would be
    stored, and the commit boundary is re-validating a decided value rather
    than deciding it late.
    """
    inv.require(Capability.CARD_WRITE, "tags")
    inv.require(Capability.CONTEXT_READ, "character")
    inv.charge(Quota.CARD_TAGS, MAX_CARD_TAG_WRITES_PER_INVOCATION, "card tag writes")
    card_id = resolve_path({"ctx": inv.ctx}, "ctx.character.id")
    if not isinstance(card_id, str) or not card_id:
        raise FlowError("card.tags.set needs a character in the invocation's context")
    tags = _scalar_list(step.tags, ns, "card.tags.set tags")
    if any(not isinstance(tag, str) for tag in tags):
        raise FlowError("card.tags.set tags must all be strings")
    inv.effects.card_tags = (card_id, normalize_tags(tags))


async def _op_branch_activate(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    """Stage a switch of the conversation's active branch.

    The message must belong to this invocation's conversation -- the same rule
    every other write follows: a write proves its target is in the invocation's
    scope. The activation itself is staged, because it is an Orb mutation and
    Orb mutations do not happen where they appear; the caller commits it under
    the conversation stream lock through the exact helper the built-in
    switch-branch route uses.
    """
    inv.require(Capability.CONVERSATION_BRANCH_ACTIVATE)
    inv.charge(Quota.BRANCH_ACTIVATE, 1, "branch activations")
    message_id = resolve_value(step.message_id, ns)
    if isinstance(message_id, bool) or not isinstance(message_id, int):
        raise FlowError("conversation.branch.activate needs an integer message id")
    if inv.host.owns_message is None:
        raise FlowError("branch activation is not available in this invocation")
    if not await inv.host.owns_message(message_id):
        raise FlowError("the selected message is not part of this conversation")
    inv.effects.branch_activation = message_id


def _op_ui_toast(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    inv.effects.toasts.append({"text": _text(step.text, ns, "ui.toast text")[:200], "tone": step.tone})


def _op_ui_invalidate(step: Any, inv: Invocation, ns: dict[str, Any]) -> None:
    inv.require(Capability.UI_CONTRIBUTE)
    if step.view not in inv.effects.invalidations:
        inv.effects.invalidations.append(step.view)


_HANDLERS: dict[str, Any] = {
    "state.get": _op_state_get,
    "state.set": _op_state_set,
    "state.delete": _op_state_delete,
    "text.concat": _op_text_concat,
    "text.replace_literal": _op_text_replace,
    "json.pick": _op_json_pick,
    "json.merge": _op_json_merge,
    "list.intersect": _op_list_intersect,
    "list.join": _op_list_join,
    "math.add": _op_math_add,
    "math.subtract": _op_math_subtract,
    "math.negate": _op_math_negate,
    "math.clamp": _op_math_clamp,
    "random.integer": _op_random_integer,
    "random.choice": _op_random_choice,
    "model.text": _op_model_text,
    "model.structured": _op_model_structured,
    "context.append": _op_context_append,
    "draft.replace": _op_draft_replace,
    "card.tags.set": _op_card_tags_set,
    "conversation.branch.activate": _op_branch_activate,
    "ui.toast": _op_ui_toast,
    "ui.invalidate": _op_ui_invalidate,
}

# Every executable operation has a handler, and every handler is an operation.
# Asserted at import so adding an operation to the contract without teaching the
# interpreter about it fails the process rather than one unlucky invocation.
_UNHANDLED = set(OPERATION_SPECS) - set(_HANDLERS) - UNIMPLEMENTED_OPS - {"if", "return", "ui.status"}
assert not _UNHANDLED, f"operations without an interpreter handler: {sorted(_UNHANDLED)}"


def unimplemented_operations(flow: Flow) -> list[str]:
    """The operations *flow* uses that this build cannot execute yet."""
    return sorted(declared_operations(flow) & UNIMPLEMENTED_OPS)


def resolve_metadata_path(metadata: Mapping[str, Any], path: str) -> Any:
    """Resolve one ``host.*`` path, for callers building availability predicates."""
    return resolve_path({"host": metadata}, path)


__all__ = [
    "DELETED",
    "UNIMPLEMENTED_OPS",
    "FlowResult",
    "HostServices",
    "Invocation",
    "ModelLane",
    "StagedEffects",
    "resolve_metadata_path",
    "run_flow",
    "unimplemented_operations",
]
