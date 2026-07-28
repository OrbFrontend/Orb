"""Snapshot-driven interactive-fragment type resolution for one turn."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..workflows import (
    MAX_EXTENSION_FRAGMENT_INSTANCES_PER_TURN,
    MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET,
    FragmentReducerBudget,
    FragmentReduceRequest,
    FragmentTypeError,
    RegistrySnapshot,
)

_INSTANCE_KEY = "_fragment_type_instance"
_MAX_STORED_VALUE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class FragmentResolution:
    fragments: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[dict[str, str], ...]
    inert_fragment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FragmentReduction:
    fields: dict[str, Any]
    progressive_fields: dict[str, Any]
    rendered_fragments: tuple[Mapping[str, Any], ...]
    diagnostics: tuple[dict[str, str], ...]


def _diagnostic(fragment: Mapping[str, Any], message: str) -> dict[str, str]:
    return {
        "fragment_id": str(fragment.get("id") or ""),
        "field_type": str(fragment.get("field_type") or ""),
        "message": message,
    }


def decode_type_config(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw or "{}")
        except ValueError:
            return None
        return dict(decoded) if isinstance(decoded, dict) else None
    return {} if raw is None else None


def resolve_fragment_instances(
    snapshot: RegistrySnapshot,
    fragments: Sequence[Mapping[str, Any]],
) -> FragmentResolution:
    """Resolve ordinary fragment instances against one captured snapshot.

    Dedicated feedback/direction-note fragments pass through unchanged. Missing
    namespaced providers and invalid configurations are diagnosed and omitted,
    never coerced into a core shape.
    """
    resolved: list[Mapping[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    inert_fragment_ids: list[str] = []
    extension_count = 0
    for raw in fragments:
        fragment = dict(raw)
        field_type = str(fragment.get("field_type") or "string")
        if field_type in ("feedback", "direction_note"):
            resolved.append(fragment)
            continue

        definition = snapshot.fragment_type(field_type)
        if definition is None:
            if ":" in field_type:
                diagnostics.append(
                    _diagnostic(
                        fragment,
                        f"fragment type provider {field_type.split(':', 1)[0]!r} is unavailable",
                    )
                )
                inert_fragment_ids.append(str(fragment.get("id") or ""))
                continue
            # Preserve the historical fallback for malformed, non-namespaced
            # card values. Global create/update routes reject these values.
            definition = snapshot.fragment_type("string")
            diagnostics.append(_diagnostic(fragment, f"unknown legacy field type {field_type!r}; using 'string'"))
            fragment["field_type"] = "string"
        assert definition is not None

        if definition.contributed:
            extension_count += 1
            if extension_count > MAX_EXTENSION_FRAGMENT_INSTANCES_PER_TURN:
                diagnostics.append(
                    _diagnostic(
                        fragment,
                        "extension-backed fragment instance limit was exceeded for this turn",
                    )
                )
                if definition.storage == "assistant_progressive":
                    inert_fragment_ids.append(str(fragment.get("id") or ""))
                continue

        config = decode_type_config(fragment.get("type_config"))
        if config is None:
            diagnostics.append(_diagnostic(fragment, "type configuration is not a JSON object"))
            if definition.storage == "assistant_progressive":
                inert_fragment_ids.append(str(fragment.get("id") or ""))
            continue
        try:
            instance = definition.instantiate(config)
        except (FragmentTypeError, ValueError) as exc:
            diagnostics.append(_diagnostic(fragment, str(exc)))
            if definition.storage == "assistant_progressive":
                inert_fragment_ids.append(str(fragment.get("id") or ""))
            continue

        property_schema = dict(instance.director_schema)
        description = str(fragment.get("description") or "")
        if description:
            property_schema["description"] = description
        fragment["type_config"] = dict(instance.config)
        fragment["director_schema"] = property_schema
        fragment["director_hint"] = definition.prompt_hint
        fragment[_INSTANCE_KEY] = instance
        resolved.append(fragment)
    return FragmentResolution(
        tuple(resolved),
        tuple(diagnostics),
        tuple(fragment_id for fragment_id in inert_fragment_ids if fragment_id),
    )


def _stored_value(value: Any) -> bool:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return len(encoded) <= _MAX_STORED_VALUE_BYTES


def with_fragment_priors(
    fragments: Sequence[Mapping[str, Any]],
    stored: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], ...], tuple[dict[str, str], ...]]:
    """Attach validated prior values and pre-rendered Director context."""
    prepared: list[Mapping[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    context_bytes = 0
    for raw in fragments:
        fragment = dict(raw)
        instance = fragment.get(_INSTANCE_KEY)
        if instance is None:
            prepared.append(fragment)
            continue

        has_previous = fragment["id"] in stored and _stored_value(stored[fragment["id"]])
        previous = stored.get(fragment["id"]) if has_previous else None
        if not has_previous and instance.has_initial:
            previous = instance.initial_value
            has_previous = True
        fragment["_has_previous"] = has_previous
        fragment["_previous_value"] = previous
        try:
            context = instance.render_prior(fragment, previous) if has_previous else ""
            if not isinstance(context, str):
                raise TypeError
            encoded_bytes = len(context.encode("utf-8"))
            if context and context_bytes + encoded_bytes > MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET:
                diagnostics.append(
                    _diagnostic(
                        fragment,
                        "the aggregate Director fragment-context budget was exceeded; prior context was omitted",
                    )
                )
                context = ""
            else:
                context_bytes += encoded_bytes
            fragment["prior_context"] = context
        except Exception:
            diagnostics.append(_diagnostic(fragment, "the fragment's prior context could not be rendered"))
            fragment["prior_context"] = ""
        prepared.append(fragment)
    return tuple(prepared), tuple(diagnostics)


async def reduce_fragment_outputs(
    fragments: Sequence[Mapping[str, Any]],
    director_fields: Mapping[str, Any],
    *,
    is_cancelled: Callable[[], bool],
    carried_progressive: Mapping[str, Any] | None = None,
) -> FragmentReduction:
    """Validate Director values, reduce them, and carry progressive state."""
    fields: dict[str, Any] = {}
    progressive = {fragment_id: value for fragment_id, value in (carried_progressive or {}).items() if _stored_value(value)}
    rendered: list[Mapping[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    budget = FragmentReducerBudget()
    writer_context_bytes = 0

    for raw in fragments:
        fragment = dict(raw)
        instance = fragment.get(_INSTANCE_KEY)
        if instance is None:
            rendered.append(fragment)
            continue

        has_previous = bool(fragment.get("_has_previous"))
        previous = fragment.get("_previous_value") if has_previous else None
        has_current = False
        current: Any = None
        if fragment["id"] in director_fields:
            director_value = director_fields[fragment["id"]]
            reason = instance.validate_director(director_value)
            if reason is None:
                try:
                    if instance.definition.contributed and not budget.can_start:
                        raise FragmentTypeError("the shared fragment-reducer budget is exhausted")
                    current = await instance.reduce_value(
                        FragmentReduceRequest(
                            fragment=_public_fragment(fragment),
                            config=instance.config,
                            previous=previous,
                            director=director_value,
                            budget=budget,
                            is_cancelled=is_cancelled,
                        )
                    )
                    has_current = _stored_value(current)
                    if not has_current:
                        diagnostics.append(_diagnostic(fragment, "the fragment reducer returned an invalid value"))
                except Exception:
                    diagnostics.append(_diagnostic(fragment, "the fragment reducer failed; the prior value was retained"))
            else:
                diagnostics.append(
                    _diagnostic(fragment, f"the Director value was invalid ({reason}); the prior value was retained")
                )

        if not has_current and instance.definition.storage == "assistant_progressive" and has_previous:
            current = previous
            has_current = True

        if has_current:
            fields[fragment["id"]] = current
            if instance.definition.storage == "assistant_progressive":
                progressive[fragment["id"]] = current
            try:
                context = instance.render_writer(fragment, previous, current)
                if not isinstance(context, str):
                    raise TypeError
                encoded_bytes = len(context.encode("utf-8"))
                if context and writer_context_bytes + encoded_bytes > MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET:
                    diagnostics.append(
                        _diagnostic(
                            fragment,
                            "the aggregate Writer fragment-context budget was exceeded; Writer context was omitted",
                        )
                    )
                    context = ""
                else:
                    writer_context_bytes += encoded_bytes
                fragment["writer_context"] = context
            except Exception:
                fragment["writer_context"] = ""
                diagnostics.append(_diagnostic(fragment, "the fragment's Writer context could not be rendered"))
        else:
            fragment["writer_context"] = ""
        rendered.append(fragment)

    return FragmentReduction(fields, progressive, tuple(rendered), tuple(diagnostics))


def _public_fragment(fragment: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in fragment.items() if not key.startswith("_") and key != "director_schema"}


def fragment_type_instance(fragment: Mapping[str, Any]):
    """Return a resolved instance for pipeline helpers, or ``None``."""
    return fragment.get(_INSTANCE_KEY)
