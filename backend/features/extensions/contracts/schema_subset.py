"""The deliberately small JSON Schema subset packages may declare.

Packages describe data in three places -- action inputs, ``model.structured``
outputs, and fragment-type config/director shapes -- and all three go through
this subset. It is an allowlist, not a filter: an unknown keyword is a
validation error, never an ignored annotation. That is the difference between
"Orb does not support ``$ref`` yet" and "``$ref`` silently did nothing, so the
author shipped a schema that constrains less than they believe".

What is supported: ``type`` (object/array/string/integer/number/boolean),
``properties``, ``required``, ``additionalProperties`` (``false`` only),
``items``, ``enum``, ``const``, ``minimum``/``maximum``,
``minLength``/``maxLength``, ``minItems``/``maxItems``, ``description``, and
``default``.

What is not, and why: ``$ref`` and ``$id`` (external resolution and cycles),
``oneOf``/``anyOf``/``allOf``/``not`` (combinatorial validation cost),
``patternProperties`` and ``pattern`` (a package-supplied regular expression is
a denial-of-service surface on Python's backtracking engine -- the same reason
there is no ``text.regex`` operation), ``additionalProperties: true`` or a
sub-schema (an object of unbounded shape defeats every size bound derived from
the declaration).

``$config`` / ``$neg_config`` are accepted in numeric keywords only, and only
where the caller opts in (a fragment type's ``director_schema``). They fill a
bound from validated integer fragment config; they are not expressions.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..limits import MAX_JSON_MEMBERS
from .common import CONFIG_KEY, LOCAL_ID_PATTERN, NEG_CONFIG_KEY

MAX_SCHEMA_DEPTH = 8
MAX_SCHEMA_PROPERTIES = 64
MAX_ENUM_MEMBERS = 64
MAX_SCHEMA_STRING_LENGTH = 8192

_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean"})

_COMMON_KEYWORDS = frozenset({"type", "description", "default", "enum", "const"})
_KEYWORDS_BY_TYPE: dict[str, frozenset[str]] = {
    "object": frozenset({"properties", "required", "additionalProperties"}),
    "array": frozenset({"items", "minItems", "maxItems"}),
    "string": frozenset({"minLength", "maxLength"}),
    "integer": frozenset({"minimum", "maximum"}),
    "number": frozenset({"minimum", "maximum"}),
    "boolean": frozenset(),
}
_NUMERIC_KEYWORDS = frozenset({"minimum", "maximum"})


class SchemaError(ValueError):
    """A declared schema is outside the supported subset."""


@dataclass(frozen=True, slots=True)
class PackageSchema:
    """A validated schema plus the config keys its numeric bounds depend on.

    ``config_keys`` is empty for every schema except a fragment type's
    ``director_schema``; it is what lets the compiler prove the referenced
    config keys exist and are integers *before* a turn tries to build the
    Director tool blob from them.
    """

    schema: Mapping[str, Any]
    config_keys: frozenset[str] = frozenset()

    def validate(self, value: Any) -> str | None:
        """Return ``None`` if *value* satisfies the schema, else one reason.

        A single reason, not a list: this string reaches a sanitized
        diagnostic, and enumerating every failure of a hostile value is work
        proportional to the attacker's input.
        """
        return _validate(self.schema, value, "value")


def parse_schema(raw: Any, *, allow_config_templates: bool = False, what: str = "schema") -> PackageSchema:
    """Validate a package-declared schema and return it normalized.

    ``additionalProperties`` is defaulted to ``false`` on every object schema,
    so a package that forgets it still gets a closed shape -- the safe default
    has to be the *implicit* one, or forgetting is the same as opting out.
    """
    config_keys: set[str] = set()
    normalized = _normalize(
        raw,
        depth=0,
        what=what,
        allow_config=allow_config_templates,
        config_keys=config_keys,
    )
    return PackageSchema(schema=normalized, config_keys=frozenset(config_keys))


def _normalize(raw: Any, *, depth: int, what: str, allow_config: bool, config_keys: set[str]) -> dict[str, Any]:
    if depth > MAX_SCHEMA_DEPTH:
        raise SchemaError(f"{what}: schema nests deeper than {MAX_SCHEMA_DEPTH}")
    if not isinstance(raw, dict):
        raise SchemaError(f"{what}: schema must be an object, got {type(raw).__name__}")

    declared_type = raw.get("type")
    if not isinstance(declared_type, str) or declared_type not in _TYPES:
        raise SchemaError(f"{what}: 'type' must be one of {sorted(_TYPES)}")

    allowed = _COMMON_KEYWORDS | _KEYWORDS_BY_TYPE[declared_type]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SchemaError(f"{what}: unsupported schema keyword(s) {unknown} for type {declared_type!r}")

    out: dict[str, Any] = {"type": declared_type}

    description = raw.get("description")
    if description is not None:
        if not isinstance(description, str) or len(description) > MAX_SCHEMA_STRING_LENGTH:
            raise SchemaError(f"{what}: 'description' must be a string of at most {MAX_SCHEMA_STRING_LENGTH} characters")
        out["description"] = description

    if "enum" in raw:
        out["enum"] = _normalize_enum(raw["enum"], what=what)
    if "const" in raw:
        _assert_plain_json(raw["const"], what=f"{what}.const")
        out["const"] = raw["const"]

    if declared_type == "object":
        properties = raw.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaError(f"{what}: 'properties' must be an object")
        if len(properties) > MAX_SCHEMA_PROPERTIES:
            raise SchemaError(f"{what}: {len(properties)} properties exceeds the limit of {MAX_SCHEMA_PROPERTIES}")
        out["properties"] = {
            name: _normalize(
                sub,
                depth=depth + 1,
                what=f"{what}.{name}",
                allow_config=allow_config,
                config_keys=config_keys,
            )
            for name, sub in properties.items()
            if _valid_property_name(name, what)
        }
        required = raw.get("required", [])
        if not isinstance(required, list) or any(not isinstance(r, str) for r in required):
            raise SchemaError(f"{what}: 'required' must be a list of property names")
        missing = [r for r in required if r not in out["properties"]]
        if missing:
            raise SchemaError(f"{what}: 'required' names undeclared propert(ies) {sorted(missing)}")
        out["required"] = list(required)
        additional = raw.get("additionalProperties", False)
        if additional is not False:
            raise SchemaError(f"{what}: 'additionalProperties' must be false (open objects are not supported in v1)")
        out["additionalProperties"] = False

    elif declared_type == "array":
        if "items" not in raw:
            raise SchemaError(f"{what}: an array schema must declare 'items'")
        out["items"] = _normalize(
            raw["items"],
            depth=depth + 1,
            what=f"{what}.items",
            allow_config=allow_config,
            config_keys=config_keys,
        )
        for key in ("minItems", "maxItems"):
            if key in raw:
                out[key] = _bounded_int(raw[key], key=key, what=what, low=0, high=MAX_JSON_MEMBERS)
        if "minItems" in out and "maxItems" in out and out["minItems"] > out["maxItems"]:
            raise SchemaError(f"{what}: minItems exceeds maxItems")

    elif declared_type == "string":
        for key in ("minLength", "maxLength"):
            if key in raw:
                out[key] = _bounded_int(raw[key], key=key, what=what, low=0, high=MAX_SCHEMA_STRING_LENGTH)
        if "minLength" in out and "maxLength" in out and out["minLength"] > out["maxLength"]:
            raise SchemaError(f"{what}: minLength exceeds maxLength")

    elif declared_type in ("integer", "number"):
        for key in _NUMERIC_KEYWORDS:
            if key in raw:
                out[key] = _numeric_bound(
                    raw[key],
                    key=key,
                    what=what,
                    allow_config=allow_config,
                    config_keys=config_keys,
                )

    if "default" in raw:
        default = raw["default"]
        _assert_plain_json(default, what=f"{what}.default")
        # A default that its own schema rejects is the failure mode this check
        # exists for: it surfaces only when a consumer omits the field, which
        # can be long after install.
        reason = _validate(out, default, f"{what}.default", skip_config_bounds=True)
        if reason is not None:
            raise SchemaError(f"{what}: 'default' does not satisfy its own schema ({reason})")
        out["default"] = default

    return out


def _valid_property_name(name: Any, what: str) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise SchemaError(f"{what}: property names must be non-empty strings of at most 64 characters")
    if name.startswith("$"):
        raise SchemaError(f"{what}: property name {name!r} uses the reserved '$' prefix")
    return True


def _normalize_enum(raw: Any, *, what: str) -> list[Any]:
    if not isinstance(raw, list) or not raw:
        raise SchemaError(f"{what}: 'enum' must be a non-empty list")
    if len(raw) > MAX_ENUM_MEMBERS:
        raise SchemaError(f"{what}: enum has {len(raw)} members, limit is {MAX_ENUM_MEMBERS}")
    for member in raw:
        _assert_plain_json(member, what=f"{what}.enum")
    return list(raw)


def _bounded_int(raw: Any, *, key: str, what: str, low: int, high: int) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise SchemaError(f"{what}: {key!r} must be an integer")
    if not low <= raw <= high:
        raise SchemaError(f"{what}: {key!r} must be between {low} and {high}")
    return raw


def _numeric_bound(raw: Any, *, key: str, what: str, allow_config: bool, config_keys: set[str]) -> Any:
    if isinstance(raw, dict):
        if not allow_config:
            raise SchemaError(f"{what}: {key!r} must be a number here ($config templates are not allowed in this schema)")
        if len(raw) != 1 or next(iter(raw)) not in (CONFIG_KEY, NEG_CONFIG_KEY):
            raise SchemaError(f"{what}: {key!r} template must be exactly one of {CONFIG_KEY!r} or {NEG_CONFIG_KEY!r}")
        name = next(iter(raw.values()))
        if not isinstance(name, str) or not re.match(LOCAL_ID_PATTERN, name):
            raise SchemaError(f"{what}: {key!r} template must name a config key using the id grammar")
        config_keys.add(name)
        return dict(raw)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SchemaError(f"{what}: {key!r} must be a number")
    if isinstance(raw, float) and not math.isfinite(raw):
        raise SchemaError(f"{what}: {key!r} must be finite")
    return raw


def _assert_plain_json(value: Any, *, what: str, depth: int = 0) -> None:
    if depth > MAX_SCHEMA_DEPTH:
        raise SchemaError(f"{what}: value nests deeper than {MAX_SCHEMA_DEPTH}")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SchemaError(f"{what}: non-finite number")
        return
    if isinstance(value, list):
        for item in value:
            _assert_plain_json(item, what=what, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SchemaError(f"{what}: object keys must be strings")
            _assert_plain_json(item, what=what, depth=depth + 1)
        return
    raise SchemaError(f"{what}: unsupported value of type {type(value).__name__}")


# ── runtime validation ───────────────────────────────────────────────────────


def _validate(
    schema: Mapping[str, Any],
    value: Any,
    path: str,
    *,
    skip_config_bounds: bool = False,
) -> str | None:
    declared = schema["type"]

    # Type check and keyword check stay together per branch, so a keyword only
    # ever runs against a value the branch has already narrowed. The earlier
    # shape -- check all types, then check all keywords -- read fine but let
    # ``len(value)`` be reached with a float on any future edit that dropped a
    # return.
    if "const" in schema and value != schema["const"]:
        return f"{path} must equal its declared const"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} is not one of the declared enum values"

    if declared == "boolean":
        return None if isinstance(value, bool) else f"{path} must be a boolean"

    if declared in ("integer", "number"):
        if isinstance(value, bool):
            return f"{path} must be an {declared}"
        if declared == "integer" and not isinstance(value, int):
            return f"{path} must be an integer"
        if declared == "number":
            if not isinstance(value, (int, float)):
                return f"{path} must be a number"
            if isinstance(value, float) and not math.isfinite(value):
                return f"{path} must be finite"
        return _numeric_bounds(schema, value, path, skip_config_bounds=skip_config_bounds)

    if declared == "string":
        if not isinstance(value, str):
            return f"{path} must be a string"
        if "minLength" in schema and len(value) < schema["minLength"]:
            return f"{path} is shorter than minLength"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{path} is longer than maxLength"
        return None

    if declared == "array":
        if not isinstance(value, list):
            return f"{path} must be an array"
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path} has fewer than minItems items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path} has more than maxItems items"
        item_schema = schema["items"]
        for index, item in enumerate(value):
            reason = _validate(item_schema, item, f"{path}[{index}]", skip_config_bounds=skip_config_bounds)
            if reason is not None:
                return reason
        return None

    if not isinstance(value, dict):
        return f"{path} must be an object"
    properties: Mapping[str, Any] = schema.get("properties", {})
    extra = sorted(set(value) - set(properties))
    if extra:
        return f"{path} has undeclared propert(ies) {extra}"
    required: Sequence[str] = schema.get("required", ())
    for name in required:
        if name not in value:
            return f"{path} is missing required property {name!r}"
    for name, item in value.items():
        reason = _validate(properties[name], item, f"{path}.{name}", skip_config_bounds=skip_config_bounds)
        if reason is not None:
            return reason
    return None


def _numeric_bounds(schema: Mapping[str, Any], value: float, path: str, *, skip_config_bounds: bool) -> str | None:
    """Check ``minimum``/``maximum``, including the unresolved-template case.

    A ``$config`` bound that survives to runtime means the per-turn schema build
    was skipped. Failing closed beats comparing a number against a dict, and
    beats treating the bound as absent -- the whole point of the template was
    that the bound exists.
    """
    for key, satisfied in (("minimum", lambda a, b: a >= b), ("maximum", lambda a, b: a <= b)):
        bound = schema.get(key)
        if bound is None:
            continue
        if isinstance(bound, dict):
            if skip_config_bounds:
                continue
            return f"{path} has an unresolved {key} template"
        if not satisfied(value, bound):
            return f"{path} violates {key}"
    return None
