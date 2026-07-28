"""Workflow-owned contracts for interactive-fragment type contributions.

Fragment types sit below both ``pipeline/`` and ``features/extensions/``:
extensions compile package declarations into :class:`FragmentTypeDefinition`
values, while the pipeline resolves those values from one captured registry
snapshot.  Nothing in this module parses manifests, reads grants or storage, or
knows about the extension interpreter.

The three ordinary built-in types use the same contract as contributed types.
``feedback`` and ``direction_note`` deliberately remain dedicated pipeline
behaviours and therefore are not entries in this catalog.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any


class FragmentTypeError(ValueError):
    """A fragment instance or contributed type could not be used safely."""


MAX_EXTENSION_FRAGMENT_INSTANCES_PER_TURN = 50
MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET = 64 * 1024
_NO_REDUCER_VALUE = object()


@dataclass(slots=True)
class FragmentReducerBudget:
    """Shared reducer work budget for one turn.

    A reducer still has the interpreter's ordinary per-flow cap.  This second
    budget prevents many individually-small fragment instances from multiplying
    that cap without bound.
    """

    max_steps: int = 512
    max_bytes: int = 256 * 1024
    steps_used: int = 0
    bytes_used: int = 0

    def charge(self, *, steps: int = 0, value: Any = _NO_REDUCER_VALUE) -> None:
        next_steps = self.steps_used + max(0, steps)
        if next_steps > self.max_steps:
            raise FragmentTypeError(f"fragment reducers exceeded the per-turn budget of {self.max_steps} steps")
        self.steps_used = next_steps
        if value is not _NO_REDUCER_VALUE:
            try:
                encoded = json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                raise FragmentTypeError("a fragment reducer returned a non-JSON value") from None
            next_bytes = self.bytes_used + len(encoded)
            if next_bytes > self.max_bytes:
                raise FragmentTypeError(f"fragment reducers exceeded the per-turn result budget of {self.max_bytes} bytes")
            self.bytes_used = next_bytes

    def charge_step(self) -> None:
        """Reserve one interpreter step before that step executes."""
        self.charge(steps=1)

    @property
    def can_start(self) -> bool:
        """Whether another contributed reducer may do useful bounded work."""
        return self.steps_used < self.max_steps and self.bytes_used < self.max_bytes


@dataclass(frozen=True, slots=True)
class FragmentReduceRequest:
    """The complete pure input to one fragment reducer."""

    fragment: Mapping[str, Any]
    config: Mapping[str, Any]
    previous: Any
    director: Any
    budget: FragmentReducerBudget
    is_cancelled: Callable[[], bool]


ValidateValue = Callable[[Any], str | None]
ReduceValue = Callable[[FragmentReduceRequest], Awaitable[Any]]
RenderPrior = Callable[[Mapping[str, Any], Any], str]
RenderWriter = Callable[[Mapping[str, Any], Any, Any], str]


@dataclass(frozen=True, slots=True)
class FragmentTypeInstance:
    """One descriptor resolved against validated per-instance configuration."""

    definition: FragmentTypeDefinition
    config: Mapping[str, Any]
    director_schema: Mapping[str, Any]
    validate_director: ValidateValue
    reduce_value: ReduceValue
    render_prior: RenderPrior
    render_writer: RenderWriter
    has_initial: bool = False
    initial_value: Any = None


InstantiateType = Callable[[Mapping[str, Any]], FragmentTypeInstance]


@dataclass(frozen=True, slots=True)
class FragmentTypeDefinition:
    """One immutable catalog entry carried by a workflow registry snapshot."""

    type_id: str
    local_id: str
    label: str
    description: str
    storage: str | None
    config_schema: Mapping[str, Any]
    instantiate: InstantiateType
    prompt_hint: str
    owner_id: str | None = None
    content_digest: str | None = None
    config_view: Mapping[str, Any] | None = None
    value_view: Mapping[str, Any] | None = None

    @property
    def contributed(self) -> bool:
        return self.owner_id is not None


def _empty_config(config: Mapping[str, Any]) -> None:
    if config:
        raise FragmentTypeError("built-in fragment types do not accept type configuration")


def _string_reason(value: Any) -> str | None:
    return None if isinstance(value, str) else "value must be a string"


def _array_reason(value: Any) -> str | None:
    if not isinstance(value, list):
        return "value must be an array"
    return None if all(isinstance(item, str) for item in value) else "array items must be strings"


async def _identity_reduce(request: FragmentReduceRequest) -> Any:
    request.budget.charge(value=request.director)
    return request.director


def _no_prior(_fragment: Mapping[str, Any], _previous: Any) -> str:
    return ""


def _progressive_prior(fragment: Mapping[str, Any], previous: Any) -> str:
    if previous is None:
        return ""
    return f"* [{fragment['id']}] ({fragment['description']}): {previous}"


def _string_writer(fragment: Mapping[str, Any], _previous: Any, current: Any) -> str:
    return f"{fragment['injection_label']}: {current}"


def _array_writer(fragment: Mapping[str, Any], _previous: Any, current: Any) -> str:
    if not isinstance(current, list):
        return ""
    return fragment["injection_label"] + ":\n" + "\n".join(f"- {item}" for item in current)


def _progressive_writer(fragment: Mapping[str, Any], previous: Any, current: Any) -> str:
    transition = f"{previous} -> {current}" if previous is not None and previous != current else str(current)
    return f"{fragment['injection_label']} ({fragment['description']}): {transition}"


def _builtin_instance(
    definition: FragmentTypeDefinition,
    config: Mapping[str, Any],
    *,
    schema: Mapping[str, Any],
    validate: ValidateValue,
    prior: RenderPrior,
    writer: RenderWriter,
) -> FragmentTypeInstance:
    _empty_config(config)
    return FragmentTypeInstance(
        definition=definition,
        config={},
        director_schema=schema,
        validate_director=validate,
        reduce_value=_identity_reduce,
        render_prior=prior,
        render_writer=writer,
    )


_EMPTY_CONFIG_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _builtin(
    type_id: str,
    label: str,
    prompt_hint: str,
    *,
    schema: Mapping[str, Any],
    validate: ValidateValue,
    storage: str | None,
    prior: RenderPrior,
    writer: RenderWriter,
) -> FragmentTypeDefinition:
    definition: FragmentTypeDefinition

    def instantiate(config: Mapping[str, Any]) -> FragmentTypeInstance:
        return _builtin_instance(
            definition,
            config,
            schema=schema,
            validate=validate,
            prior=prior,
            writer=writer,
        )

    definition = FragmentTypeDefinition(
        type_id=type_id,
        local_id=type_id,
        label=label,
        description="Built in to Orb.",
        storage=storage,
        config_schema=_EMPTY_CONFIG_SCHEMA,
        instantiate=instantiate,
        prompt_hint=prompt_hint,
    )
    return definition


BUILTIN_FRAGMENT_TYPES: Mapping[str, FragmentTypeDefinition] = {
    "string": _builtin(
        "string",
        "Single value",
        "single value",
        schema={"type": "string"},
        validate=_string_reason,
        storage=None,
        prior=_no_prior,
        writer=_string_writer,
    ),
    "array": _builtin(
        "array",
        "List",
        "list of strings",
        schema={"type": "array", "items": {"type": "string"}},
        validate=_array_reason,
        storage=None,
        prior=_no_prior,
        writer=_array_writer,
    ),
    "progressive": _builtin(
        "progressive",
        "Progressive",
        "single value, evolves across turns",
        schema={"type": "string"},
        validate=_string_reason,
        storage="assistant_progressive",
        prior=_progressive_prior,
        writer=_progressive_writer,
    ),
}
