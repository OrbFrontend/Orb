"""Shared contract primitives: the strict model base, identifiers, dynamic
values, and the predicate AST.

Two rules shape everything in this package.

**Unknown fields are errors, never data.** Every model forbids extras. A v1
package that carries a field this build does not understand is rejected at
parse time rather than installed with the field silently dropped -- otherwise
"install succeeded" would mean "some of your package was ignored", and a future
Orb that *does* understand the field would change the package's behavior
without an update.

**Package strings are values, never code.** A ``$ref`` is a path into a
host-owned namespace, resolved by a validated resolver. A ``$template`` does
path substitution and nothing else: no filters, no property access outside the
resolver, no calls, no loops, no includes, no expressions. There is no form in
this grammar that evaluates a package string.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

from ..limits import (
    MAX_JSON_DEPTH,
    MAX_LABEL_CHARS,
    MAX_PREDICATE_DEPTH,
    MAX_REF_PATH_SEGMENTS,
    MAX_TEMPLATE_CHARS,
    MAX_TEMPLATE_SUBSTITUTIONS,
)
from ..paths import normalize_package_path


class ExtModel(BaseModel):
    """Base for every package contract model.

    ``strict=True`` disables Pydantic's coercion, so ``"3"`` never becomes ``3``
    and ``1`` never becomes ``True``. A package's declared types are then
    exactly what the compiler sees, which is what makes the canonical encoding
    of a manifest a faithful description of the installed contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)


# ── identifiers ──────────────────────────────────────────────────────────────
# The lowercase fragment/card grammar, reused so an extension id, a local
# fragment-type id, and a card-embedded reference are all the same shape.

EXTENSION_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
LOCAL_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
STEP_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

ExtensionId = Annotated[str, StringConstraints(pattern=EXTENSION_ID_PATTERN)]
LocalId = Annotated[str, StringConstraints(pattern=LOCAL_ID_PATTERN)]
StepId = Annotated[str, StringConstraints(pattern=STEP_ID_PATTERN)]
Label = Annotated[str, StringConstraints(min_length=1, max_length=MAX_LABEL_CHARS)]


def _validate_package_path(raw: str) -> str:
    return normalize_package_path(raw, what="package path")


PackagePath = Annotated[str, AfterValidator(_validate_package_path)]
"""A relative, normalized, collision-checked path into this package's content.

Every field that names a file uses this -- a manifest hook, a view source, an
``asset`` media reference. There is no second path type with laxer rules,
because a path that skipped normalization would be the one place a package
string could escape the content store.
"""


# No leading underscore: the resolver does no attribute access, but a path that
# *looks* like `ctx.draft.__class__` is the first thing anyone tries against a
# future resolver change, and there is no legitimate host field that needs one.
_SEGMENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_INDEX_RE = re.compile(r"^(0|[1-9][0-9]{0,3})$")

VALUE_ROOTS = frozenset({"ctx", "input", "host", "steps", "fragment", "data", "config", "state", "item"})
"""Namespaces a ``$ref`` may address.

Flow namespaces: ``ctx`` is the capability-filtered invocation context,
``input`` the validated action input, ``host`` the invocation metadata
projection, ``steps`` the output of an *earlier* named step, and ``fragment``
the reducer's config/previous/director projection.

View namespaces: ``data`` holds one entry per declared view source, ``config``
and ``state`` this extension's own slots, and ``item`` the current row while a
list or table renders its children.

The two sets share one root vocabulary rather than splitting the value grammar
in two, so a ``$ref`` means the same thing wherever it appears. A namespace
that is not bound in the current position resolves to the missing sentinel --
the same answer an absent key gives -- rather than to a second kind of error.

There is deliberately no root for settings, endpoints, the database, or another
extension.
"""


def validate_ref_path(path: object, *, what: str = "$ref") -> str:
    """Validate a dotted value path and return it unchanged.

    Shape only: that the root is a known namespace and every segment is an
    identifier or a small array index. Whether ``steps.score`` exists, or
    whether ``ctx.draft`` was granted, is the compiler's job -- it needs the
    whole flow and the grant set to answer, and this runs on one string.
    """
    if not isinstance(path, str):
        raise ValueError(f"{what} must be a string, got {type(path).__name__}")
    segments = path.split(".")
    if len(segments) < 2:
        raise ValueError(f"{what} {path!r} must name at least a root and one segment")
    if len(segments) > MAX_REF_PATH_SEGMENTS:
        raise ValueError(f"{what} {path!r} has more than {MAX_REF_PATH_SEGMENTS} segments")
    root, rest = segments[0], segments[1:]
    if root not in VALUE_ROOTS:
        raise ValueError(f"{what} {path!r} must start with one of {sorted(VALUE_ROOTS)}")
    for segment in rest:
        if not (_SEGMENT_RE.match(segment) or _INDEX_RE.match(segment)):
            raise ValueError(f"{what} {path!r} has an invalid segment {segment!r}")
    return path


_TEMPLATE_HOLE_RE = re.compile(r"\{\{([^{}]*)\}\}")


def template_paths(text: str) -> list[str]:
    """The paths a validated template substitutes, in order of appearance."""
    return [hole.strip() for hole in _TEMPLATE_HOLE_RE.findall(text)]


def validate_template(text: object, *, what: str = "$template") -> str:
    """Validate a substitution template and return it unchanged.

    ``{{path}}`` holes are substituted with the scalar at *path*. Everything
    else is literal text. A stray ``{{`` or ``}}`` is an error rather than
    literal output: a template whose braces do not balance is more likely a
    package author expecting an expression language than someone who meant to
    print a brace.
    """
    if not isinstance(text, str):
        raise ValueError(f"{what} must be a string, got {type(text).__name__}")
    if len(text) > MAX_TEMPLATE_CHARS:
        raise ValueError(f"{what} is {len(text)} characters, limit is {MAX_TEMPLATE_CHARS}")
    holes = _TEMPLATE_HOLE_RE.findall(text)
    if len(holes) > MAX_TEMPLATE_SUBSTITUTIONS:
        raise ValueError(f"{what} has {len(holes)} substitutions, limit is {MAX_TEMPLATE_SUBSTITUTIONS}")
    for hole in holes:
        validate_ref_path(hole.strip(), what=f"{what} substitution")
    stripped = _TEMPLATE_HOLE_RE.sub("", text)
    if "{{" in stripped or "}}" in stripped:
        raise ValueError(f"{what} has an unbalanced '{{{{' or '}}}}'")
    return text


# ── dynamic values ───────────────────────────────────────────────────────────

REF_KEY = "$ref"
TEMPLATE_KEY = "$template"
SECRET_KEY = "$secret"
CONFIG_KEY = "$config"
NEG_CONFIG_KEY = "$neg_config"

_RESERVED_KEYS = frozenset({REF_KEY, TEMPLATE_KEY, SECRET_KEY, CONFIG_KEY, NEG_CONFIG_KEY})


def _check_value(value: Any, *, depth: int = 0, what: str = "value") -> Any:
    if depth > MAX_JSON_DEPTH:
        raise ValueError(f"{what} nests deeper than {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{what} contains a non-finite number")
        return value
    if isinstance(value, list):
        for item in value:
            _check_value(item, depth=depth + 1, what=what)
        return value
    if isinstance(value, dict):
        keys = set(value)
        reserved = keys & _RESERVED_KEYS
        if reserved:
            if keys != reserved or len(reserved) != 1:
                raise ValueError(f"{what}: a {sorted(reserved)[0]} form must be the object's only key")
            key = next(iter(reserved))
            if key == REF_KEY:
                validate_ref_path(value[key])
                return value
            if key == TEMPLATE_KEY:
                validate_template(value[key])
                return value
            raise ValueError(f"{what}: {key} is not allowed in this position")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{what}: object keys must be strings")
            if key.startswith("$"):
                raise ValueError(f"{what}: object key {key!r} uses the reserved '$' prefix")
            _check_value(item, depth=depth + 1, what=what)
        return value
    raise ValueError(f"{what}: unsupported value of type {type(value).__name__}")


Value = Annotated[Any, AfterValidator(_check_value)]
"""A JSON literal, a ``$ref``, a ``$template``, or a container of those.

Reserved ``$`` keys are exclusive: ``{"$ref": "ctx.draft", "extra": 1}`` is a
validation error rather than a ref with an ignored sibling, and no ordinary
object key may start with ``$`` -- which is what keeps a future ``$`` form from
being expressible as data in an already-installed package.
"""


def _check_secret_value(value: Any) -> Any:
    """A value position that may also carry ``{"$secret": "name"}``.

    Only ``http.request`` header and body positions use this. A secret
    reference compiles to an opaque request substitution: it is not an ordinary
    flow value, so it can never reach a URL, a template, a log, state, the UI,
    an SSE payload, or a returned error.
    """
    if isinstance(value, dict) and SECRET_KEY in value:
        if set(value) != {SECRET_KEY}:
            raise ValueError("a $secret form must be the object's only key")
        name = value[SECRET_KEY]
        if not isinstance(name, str) or not re.match(LOCAL_ID_PATTERN, name):
            raise ValueError("$secret must name a declared secret using the id grammar")
        return value
    if isinstance(value, dict):
        for item in value.values():
            _check_secret_value(item)
        return _check_value(value)
    if isinstance(value, list):
        for item in value:
            _check_secret_value(item)
        return value
    return _check_value(value)


SecretBearingValue = Annotated[Any, AfterValidator(_check_secret_value)]


# ── predicates ───────────────────────────────────────────────────────────────

_BINARY_PREDICATES = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
_LIST_PREDICATES = frozenset({"and", "or"})
PREDICATE_OPS = _BINARY_PREDICATES | _LIST_PREDICATES | {"exists", "not"}


def _check_predicate(node: Any, *, depth: int = 0) -> Any:
    """Validate one node of the structured predicate AST.

    The AST exists so ``when`` can be evaluated without an expression parser.
    Every node is a single-key object naming a fixed operator; there is no
    string form to parse, so there is nothing to escape.
    """
    if depth > MAX_PREDICATE_DEPTH:
        raise ValueError(f"predicate nests deeper than {MAX_PREDICATE_DEPTH}")
    if not isinstance(node, dict) or len(node) != 1:
        raise ValueError("a predicate must be an object with exactly one operator key")
    op, operand = next(iter(node.items()))
    if op not in PREDICATE_OPS:
        raise ValueError(f"unknown predicate operator {op!r}; expected one of {sorted(PREDICATE_OPS)}")
    if op in _BINARY_PREDICATES:
        if not isinstance(operand, list) or len(operand) != 2:
            raise ValueError(f"predicate {op!r} takes exactly two operands")
        for item in operand:
            _check_value(item, what=f"predicate {op!r} operand")
        return node
    if op in _LIST_PREDICATES:
        if not isinstance(operand, list) or not operand:
            raise ValueError(f"predicate {op!r} takes a non-empty list of predicates")
        for item in operand:
            _check_predicate(item, depth=depth + 1)
        return node
    if op == "not":
        _check_predicate(operand, depth=depth + 1)
        return node
    _check_value(operand, what="predicate 'exists' operand")
    return node


Predicate = Annotated[dict, AfterValidator(_check_predicate)]
