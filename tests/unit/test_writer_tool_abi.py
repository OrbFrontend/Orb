"""The core Writer-tool ABI: name derivation, id validation, result encoding.

Three layers that cannot import one another have to agree on these values
exactly, so the tests are about *identity* rather than about behavior: the same
key must always produce the same provider-facing name, a name outside the
derived namespace must never look like one, and the JSON a Writer reads back
must be built from a closed vocabulary rather than from whatever a caller
passed.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from backend.core.writer_tools import (
    MAX_WIRE_NAME_CHARS,
    RESOLVER_UNAVAILABLE,
    WRITER_TOOL_PREFIX,
    WriterToolError,
    WriterToolKey,
    WriterToolSpec,
    is_writer_tool_name,
    valid_call_id,
    wire_name,
    writer_tool_error,
    writer_tool_ok,
)


def test_a_name_is_derived_from_the_key_and_is_stable():
    key = WriterToolKey(owner_id="outcome-resolver", local_id="resolve_outcome")
    assert wire_name(key) == f"{WRITER_TOOL_PREFIX}outcome-resolver--resolve_outcome"
    assert wire_name(key) == wire_name(WriterToolKey("outcome-resolver", "resolve_outcome"))


def test_two_different_keys_cannot_derive_one_name():
    """Doubled hyphens inside either valid id take the injective escaped form."""
    first = wire_name(WriterToolKey("a", "b--c"))
    second = wire_name(WriterToolKey("a--b", "c"))
    assert first != second
    assert first == f"{WRITER_TOOL_PREFIX}_1_a_b--c"
    assert second == f"{WRITER_TOOL_PREFIX}_4_a--b_c"
    assert is_writer_tool_name(first)
    assert is_writer_tool_name(second)


@pytest.mark.parametrize(
    "key",
    [
        WriterToolKey("Scene-Meter", "x"),  # uppercase is not the id grammar
        WriterToolKey("scene meter", "x"),
        WriterToolKey("scene", "Resolve"),
        WriterToolKey("", "x"),
        WriterToolKey("scene", ""),
        WriterToolKey("scene:meter", "x"),
        WriterToolKey("../evil", "x"),
    ],
)
def test_a_name_outside_the_id_grammar_is_refused(key):
    with pytest.raises(WriterToolError):
        wire_name(key)


def test_an_overlong_pair_is_refused_at_derivation():
    """Either id alone satisfies the grammar; only their combination overflows,
    which is why the check cannot live on the id type."""
    key = WriterToolKey("a" * 40, "b" * 40)
    with pytest.raises(WriterToolError, match=str(MAX_WIRE_NAME_CHARS)):
        wire_name(key)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (f"{WRITER_TOOL_PREFIX}a--b", True),
        ("direct_scene", False),
        ("editor_apply_patch", False),
        ("orb_writer_", False),  # prefix alone is not a name
        (f"{WRITER_TOOL_PREFIX}a b", False),
        (None, False),
        (123, False),
    ],
)
def test_the_derived_namespace_is_recognisable(name, expected):
    assert is_writer_tool_name(name) is expected


def test_no_builtin_tool_name_is_in_the_writer_namespace():
    """The two registries never consult each other; the prefix is what keeps
    them disjoint."""
    from backend.inference import BUILTIN_TOOL_NAMES

    assert not any(is_writer_tool_name(name) for name in BUILTIN_TOOL_NAMES)


def test_a_spec_recursively_freezes_its_schema_and_returns_plain_copies():
    source: dict[str, Any] = {
        "type": "function",
        "function": {
            "name": f"{WRITER_TOOL_PREFIX}a--b",
            "parameters": {"type": "object", "required": ["value"]},
        },
    }
    spec = WriterToolSpec(
        key=WriterToolKey("a", "b"),
        wire_name=f"{WRITER_TOOL_PREFIX}a--b",
        label="B",
        schema=source,
    )

    source["function"]["name"] = "mutated"
    source["function"]["parameters"]["required"].append("other")
    assert spec.schema["function"]["name"] == f"{WRITER_TOOL_PREFIX}a--b"
    assert spec.schema["function"]["parameters"]["required"] == ("value",)

    with pytest.raises(TypeError):
        cast(dict[str, Any], spec.schema["function"])["name"] = "mutated"

    plain = spec.provider_schema()
    plain["function"]["name"] = "copy-only"
    assert spec.schema["function"]["name"] == f"{WRITER_TOOL_PREFIX}a--b"


@pytest.mark.parametrize(
    ("call_id", "expected"),
    [
        ("call_abc123", True),
        ("call-1", True),
        ("toolu_01A:b.c", True),
        ("", False),
        ("   ", False),
        ("call id", False),
        ("call\n1", False),
        ("x" * 129, False),
        (None, False),
    ],
)
def test_only_a_replayable_call_id_is_accepted(call_id, expected):
    assert valid_call_id(call_id) is expected


def test_the_success_payload_carries_the_value_and_nothing_else():
    assert writer_tool_ok({"outcome": "success"}) == {"status": "ok", "result": {"outcome": "success"}}


def test_an_unknown_error_code_collapses_rather_than_reaching_the_model():
    """A closed vocabulary a caller could widen by passing a string would not be
    closed; raising would turn a recoverable extension failure into a turn
    failure."""
    assert writer_tool_error("boom")["code"] == RESOLVER_UNAVAILABLE
    assert writer_tool_error(RESOLVER_UNAVAILABLE)["code"] == RESOLVER_UNAVAILABLE
