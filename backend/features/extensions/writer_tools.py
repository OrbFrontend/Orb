"""Turning an API 2 Writer-tool declaration into the bytes a provider sees.

One module builds the provider tool entry, and the compiler and the adapter
both call it. That matters more here than in most places: the schema built at
compile time is what the byte budget was checked against, and the schema
published into a registry snapshot is what a turn sends. If those were two
constructions the budget would be advisory, and a package could pass
compilation with one blob and ship another.

Nothing here reads a grant, a database row, or a registry. It converts a
validated descriptor into an immutable :class:`~backend.core.writer_tools.WriterToolSpec`
and refuses one that would not fit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ...core import WriterToolKey, WriterToolSpec, wire_name
from .contracts import WriterToolDescriptor, parse_schema
from .digest import canonical_json_bytes
from .errors import PackageValidationError
from .limits import MAX_WRITER_TOOL_SCHEMA_BYTES


def build_writer_tool_spec(
    *,
    extension_id: str,
    descriptor: WriterToolDescriptor,
    content_digest: str = "",
) -> WriterToolSpec:
    """Compile one declared Writer tool into its immutable spec.

    The schema is the ordinary OpenAI-style function entry, assembled by Orb
    from the package's *description and input shape* -- never copied from a
    package-supplied blob. A package therefore cannot smuggle a provider
    keyword Orb's schema subset does not model, because the only keys in the
    result are the ones written here.

    Raises :class:`~.errors.PackageValidationError` when the encoded entry is
    over budget. It is a compile-time failure by design: the blob is prompt
    bytes the user did not author, and discovering the overflow on the first
    turn would mean a provider rejection in the middle of a reply.
    """
    name = wire_name(WriterToolKey(owner_id=extension_id, local_id=descriptor.id))
    parameters = dict(parse_schema(descriptor.input_schema, what="writer_tool input_schema").schema)
    schema: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": name,
            "description": descriptor.description,
            "parameters": parameters,
        },
    }
    size = len(canonical_json_bytes(schema))
    if size > MAX_WRITER_TOOL_SCHEMA_BYTES:
        raise PackageValidationError(
            f"writer_tool {descriptor.id!r} compiles to a {size}-byte tool schema, over the "
            f"{MAX_WRITER_TOOL_SCHEMA_BYTES} byte limit; shorten its description or its input schema"
        )
    return WriterToolSpec(
        key=WriterToolKey(owner_id=extension_id, local_id=descriptor.id),
        wire_name=name,
        label=descriptor.label,
        schema=schema,
        content_digest=content_digest,
    )


def writer_tool_output_schema(descriptor: WriterToolDescriptor) -> Any:
    """The compiled schema a Writer-tool flow's return value is checked against."""
    return parse_schema(descriptor.output_schema, what="writer_tool output_schema")


def writer_tool_input_schema(descriptor: WriterToolDescriptor) -> Any:
    """The compiled schema the model's arguments are checked against."""
    return parse_schema(descriptor.input_schema, what="writer_tool input_schema")


def schema_bytes(schema: Mapping[str, Any]) -> int:
    """Canonical encoded size of one tool entry, for aggregate blob budgets."""
    return len(canonical_json_bytes(dict(schema)))


__all__ = [
    "build_writer_tool_spec",
    "schema_bytes",
    "writer_tool_input_schema",
    "writer_tool_output_schema",
]
