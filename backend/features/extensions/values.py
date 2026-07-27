"""Runtime resolution of the value grammar: ``$ref``, ``$template``, predicates.

The contract layer proved these *shapes* are well-formed at parse time. This
module evaluates them against one invocation's namespaces, and it is the only
place a package string is turned into a value.

Three rules carry the security weight:

* **Resolution is dictionary lookup, never attribute access.** A path segment
  indexes a ``Mapping`` key or a ``Sequence`` position. ``ctx.draft.__class__``
  resolves to :data:`MISSING`, not to a Python type, because nothing here calls
  ``getattr``.
* **Missing is a value, not an absence.** An unresolved optional path becomes
  the :data:`MISSING` sentinel rather than ``None``, so ``exists`` is
  unambiguous and ``null`` stays a value a package can legitimately store. The
  sentinel may be tested or replaced by a ``fallback``; it can never be
  returned, persisted, interpolated, or handed to a host operation -- every
  sink calls :func:`assert_concrete` first.
* **Templates substitute and nothing else.** ``{{path}}`` is replaced by the
  scalar at *path*. There are no filters, no calls, no loops, no includes, no
  expressions -- so there is no syntax in a template for an attacker to reach
  for and nothing that needs escaping.

Comparisons are type-strict: ``1`` never equals ``True`` and ``"1"`` never
equals ``1``. Python's own ``==`` disagrees on the first of those, and a
predicate whose meaning depends on that quirk is a predicate no package author
can reason about.
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from .errors import FlowError
from .limits import (
    MAX_JSON_DEPTH,
    MAX_JSON_MEMBERS,
    MAX_JSON_STRING_BYTES,
    MAX_RENDERED_TEMPLATE_CHARS,
)


class _Missing:
    """The distinct 'this path resolved to nothing' value."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()

_INDEX_RE = re.compile(r"^(0|[1-9][0-9]{0,3})$")
_TEMPLATE_HOLE_RE = re.compile(r"\{\{([^{}]*)\}\}")

REF_KEY = "$ref"
TEMPLATE_KEY = "$template"
SECRET_KEY = "$secret"


def resolve_path(namespaces: Mapping[str, Any], path: str) -> Any:
    """Resolve a dotted value path, or return :data:`MISSING`.

    Walks ``Mapping`` keys and ``Sequence`` positions only. A path that runs
    off the end of the data -- or into a scalar, or past a list's length --
    resolves to :data:`MISSING` rather than raising, because "the draft has no
    third paragraph" is ordinary flow control, not an error.
    """
    current: Any = namespaces
    for segment in path.split("."):
        if isinstance(current, Mapping):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, (list, tuple)) and _INDEX_RE.match(segment):
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
        if current is MISSING:
            return MISSING
    return current


def resolve_value(value: Any, namespaces: Mapping[str, Any], *, depth: int = 0) -> Any:
    """Evaluate one declared value against *namespaces*.

    Containers are rebuilt rather than mutated, so a resolved value never
    aliases a namespace the caller still owns -- a flow that stored its own
    ``ctx`` fragment into state and then mutated it would otherwise rewrite the
    context the next step reads.
    """
    if depth > MAX_JSON_DEPTH:
        raise FlowError("value nests too deeply to resolve")
    if isinstance(value, dict):
        if set(value) == {REF_KEY}:
            return resolve_path(namespaces, value[REF_KEY])
        if set(value) == {TEMPLATE_KEY}:
            return render_template(value[TEMPLATE_KEY], namespaces)
        if len(value) > MAX_JSON_MEMBERS:
            raise FlowError("object has too many members")
        return {key: resolve_value(item, namespaces, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_JSON_MEMBERS:
            raise FlowError("array has too many members")
        return [resolve_value(item, namespaces, depth=depth + 1) for item in value]
    return value


def render_template(text: str, namespaces: Mapping[str, Any]) -> str:
    """Substitute ``{{path}}`` holes with the scalar at each path.

    A hole that resolves to a container or to :data:`MISSING` fails the step:
    silently rendering ``[object]`` or an empty string would let a flow build a
    prompt or a state value out of a gap it never noticed.
    """

    def substitute(match: re.Match[str]) -> str:
        path = match.group(1).strip()
        resolved = resolve_path(namespaces, path)
        if resolved is MISSING:
            raise FlowError(f"template substitution {path!r} resolved to nothing")
        return scalar_text(resolved, what=f"template substitution {path!r}")

    rendered = _TEMPLATE_HOLE_RE.sub(substitute, text)
    if len(rendered) > MAX_RENDERED_TEMPLATE_CHARS:
        raise FlowError(f"rendered template exceeds {MAX_RENDERED_TEMPLATE_CHARS} characters")
    return rendered


def scalar_text(value: Any, *, what: str) -> str:
    """Render a JSON scalar as text, rejecting containers and sentinels.

    ``True`` renders as ``true`` rather than Python's ``True``: the value came
    from JSON and goes back into a prompt or a template, so the JSON spelling
    is the one the package author wrote and the model will read.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise FlowError(f"{what} is a non-finite number")
        return repr(value)
    if value is None:
        return "null"
    raise FlowError(f"{what} is not a scalar")


def assert_concrete(value: Any, *, what: str) -> Any:
    """Reject :data:`MISSING` reaching a sink, returning *value* otherwise.

    Called by every operation that persists, returns, interpolates, or
    transmits. The sentinel exists to be *tested*; letting one escape would put
    a host-private object into package-visible data.
    """
    if value is MISSING:
        raise FlowError(f"{what} resolved to nothing")
    if isinstance(value, dict):
        for item in value.values():
            assert_concrete(item, what=what)
    elif isinstance(value, list):
        for item in value:
            assert_concrete(item, what=what)
    return value


# ── predicates ───────────────────────────────────────────────────────────────


def evaluate_predicate(node: Mapping[str, Any], namespaces: Mapping[str, Any]) -> bool:
    """Evaluate one validated predicate node to a bool.

    Total: every operator is defined for every pair of JSON values. A
    comparison between incomparable types is ``False``, not an error, because a
    predicate that could *raise* would make ``when`` a way to fail a flow from
    inside a guard that was supposed to prevent work.
    """
    op, operand = next(iter(node.items()))
    if op == "and":
        return all(evaluate_predicate(item, namespaces) for item in operand)
    if op == "or":
        return any(evaluate_predicate(item, namespaces) for item in operand)
    if op == "not":
        return not evaluate_predicate(operand, namespaces)
    if op == "exists":
        return resolve_value(operand, namespaces) is not MISSING
    left = resolve_value(operand[0], namespaces)
    right = resolve_value(operand[1], namespaces)
    if op == "eq":
        return values_equal(left, right)
    if op == "ne":
        return not values_equal(left, right)
    return _ordered(op, left, right)


def values_equal(left: Any, right: Any) -> bool:
    """Type-strict equality. ``MISSING`` equals only ``MISSING``.

    Public because ``list.intersect`` needs membership to mean exactly what
    ``eq`` means. Two equality notions in one language -- one for predicates and
    one for set operations -- would disagree on ``1`` versus ``True`` the first
    time a model returned the wrong one.
    """
    if left is MISSING or right is MISSING:
        return left is right
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    return left == right


_ORDER_OPS: dict[str, Callable[[Any, Any], Any]] = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
}


def _ordered(op: str, left: Any, right: Any) -> bool:
    """Numeric and string ordering; anything else compares ``False``.

    The two comparable shapes are checked before the operator runs, so the
    operator itself never sees a pair Python would raise on -- ``"a" < 1``
    returns ``False`` here rather than escaping as a ``TypeError`` from inside
    a ``when`` guard. Booleans are excluded from the numeric branch for the same
    reason equality is type-strict: ``True > 0`` is a fact about Python's int
    subclass, not about the package's data.
    """
    numeric = isinstance(left, (int, float)) and isinstance(right, (int, float))
    boolish = isinstance(left, bool) or isinstance(right, bool)
    both_strings = isinstance(left, str) and isinstance(right, str)
    if not ((numeric and not boolish) or both_strings):
        return False
    return bool(_ORDER_OPS[op](left, right))


# ── JSON value bounds ────────────────────────────────────────────────────────


def assert_json_bounds(value: Any, *, what: str, depth: int = 0) -> Any:
    """Enforce the shared JSON bounds on a value a flow produced.

    A value that could not have been *authored* in a package must also not be
    synthesizable at runtime: same depth, member, and finiteness rules as the
    parser, applied to model output, HTTP responses, and computed containers
    before any of them becomes state, a draft, or a return value.
    """
    if depth > MAX_JSON_DEPTH:
        raise FlowError(f"{what} nests deeper than {MAX_JSON_DEPTH}")
    if isinstance(value, dict):
        if len(value) > MAX_JSON_MEMBERS:
            raise FlowError(f"{what} has more than {MAX_JSON_MEMBERS} members")
        for key, item in value.items():
            if not isinstance(key, str):
                raise FlowError(f"{what} has a non-string object key")
            if len(key.encode("utf-8")) > MAX_JSON_STRING_BYTES:
                raise FlowError(f"{what} has an object key over the {MAX_JSON_STRING_BYTES} byte string limit")
            assert_json_bounds(item, what=what, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_MEMBERS:
            raise FlowError(f"{what} has more than {MAX_JSON_MEMBERS} members")
        for item in value:
            assert_json_bounds(item, what=what, depth=depth + 1)
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_JSON_STRING_BYTES:
            raise FlowError(f"{what} contains a string over the {MAX_JSON_STRING_BYTES} byte limit")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise FlowError(f"{what} contains a non-finite number")
    elif not isinstance(value, (int, bool)) and value is not None:
        raise FlowError(f"{what} contains an unsupported value")
    return value


def sequence_or_empty(value: Any) -> Sequence[Any]:
    """Read a value as a sequence, or an empty one. Never raises."""
    return value if isinstance(value, (list, tuple)) else ()
