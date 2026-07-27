"""The Phase 3 operations: `list.intersect`, `list.join`, and `card.tags.set`.

Three small operations with one shared property worth testing directly: each is
a *single bounded* host-owned step with no per-element package logic, which is
what keeps the two list operations from being the seed of a collection library
and keeps the tag write to one card by construction rather than by quota.

The template rule is tested here too, in the negative: interpolating an array
must fail as a plain scalar violation. The scalar-array rendering exception was
specified once and withdrawn in favour of `list.join`; a test that array
interpolation still fails is how it stays withdrawn.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core import MAX_TAG_BYTES, MAX_TAGS_PER_CARD, normalize_tags
from backend.features.extensions.contracts import Flow, OpContext
from backend.features.extensions.errors import FlowError
from backend.features.extensions.interpreter import (
    FlowResult,
    HostServices,
    Invocation,
    run_flow,
)
from backend.features.extensions.limits import MAX_LIST_OPERATION_MEMBERS
from backend.features.extensions.values import render_template

GRANTS = frozenset(
    {
        ("state.read", None),
        ("state.read", "config"),
        ("state.write", None),
        ("state.write", "character"),
        ("card.tags.write", None),
        ("context.character.read", None),
    }
)


def flow(*steps) -> Flow:
    return Flow.model_validate({"flow_version": 1, "steps": list(steps)})


async def run(f: Flow, *, ctx: dict | None = None, grants=GRANTS, state: dict | None = None) -> FlowResult:
    stored = dict(state or {})

    async def read_state(scope: str):
        return stored

    invocation = Invocation(
        extension_id="tag-librarian",
        context=OpContext.ACTION,
        host=HostServices(grants=lambda: grants, read_state=read_state),
        ctx=ctx or {},
        scopes_in_scope=frozenset({"config", "character"}),
        seed="test",
    )
    result: FlowResult | None = None
    async for item in run_flow(f, invocation):
        if isinstance(item, FlowResult):
            result = item
    assert result is not None
    return result


# ── list.intersect ──────────────────────────────────────────────────────────


async def test_intersect_keeps_first_array_order_and_drops_the_rest():
    result = await run(
        flow(
            {
                "id": "kept",
                "op": "list.intersect",
                "value": ["noir", "invented", "detective", "noir"],
                "allowed": ["detective", "noir", "unused"],
            },
            {"op": "return", "value": {"$ref": "steps.kept"}},
        )
    )
    # Value order, deduplicated: "what the model chose, minus what it invented".
    assert result.value == ["noir", "detective"]


async def test_intersect_membership_is_type_strict():
    """The same equality `eq` uses. Two notions would disagree on 1 vs True."""
    result = await run(
        flow(
            {"id": "kept", "op": "list.intersect", "value": [1, True], "allowed": [True]},
            {"op": "return", "value": {"$ref": "steps.kept"}},
        )
    )
    assert result.value == [True]


async def test_intersect_rejects_an_oversized_array():
    with pytest.raises(FlowError, match="more than"):
        await run(
            flow(
                {
                    "op": "list.intersect",
                    "id": "kept",
                    "value": ["x"] * (MAX_LIST_OPERATION_MEMBERS + 1),
                    "allowed": ["x"],
                }
            )
        )


async def test_intersect_rejects_container_members():
    with pytest.raises(FlowError, match="only scalars"):
        await run(flow({"op": "list.intersect", "id": "k", "value": [{"a": 1}], "allowed": ["a"]}))


# ── list.join ───────────────────────────────────────────────────────────────


async def test_join_uses_the_declared_separator():
    result = await run(
        flow(
            {"id": "text", "op": "list.join", "value": ["a", "b", 3], "separator": "; "},
            {"op": "return", "value": {"$ref": "steps.text"}},
        )
    )
    assert result.value == "a; b; 3"


def test_join_rejects_a_separator_outside_the_closed_set():
    """A free-form separator is one argument away from a format language."""
    with pytest.raises(ValidationError):
        flow({"op": "list.join", "id": "t", "value": ["a"], "separator": " | "})


async def test_join_rejects_an_oversized_or_nonscalar_array():
    with pytest.raises(FlowError):
        await run(flow({"op": "list.join", "id": "t", "value": ["x"] * (MAX_LIST_OPERATION_MEMBERS + 1)}))
    with pytest.raises(FlowError, match="only scalars"):
        await run(flow({"op": "list.join", "id": "t", "value": [["nested"]]}))


def test_a_template_interpolating_an_array_still_fails():
    """The withdrawn exception stays withdrawn.

    An earlier draft rendered scalar arrays joined by a frozen ``", "``. That
    was replaced by `list.join`, whose separator comes from a closed host-owned
    set — so templates are back to exactly one rule with no special cases, and
    an array in a hole is a plain scalar violation.
    """
    with pytest.raises(FlowError, match="not a scalar"):
        render_template("{{ctx.tags}}", {"ctx": {"tags": ["a", "b"]}})


# ── card.tags.set ───────────────────────────────────────────────────────────


def test_a_flow_declaring_a_card_argument_fails_compilation():
    """The operation's blast radius is one card *by construction*.

    There is no ``card_id`` field, so a package that declares one is rejected at
    parse time rather than having the field quietly ignored — which is the
    difference between a scope rule and a naming convention.
    """
    with pytest.raises(ValidationError):
        flow({"op": "card.tags.set", "tags": ["noir"], "card_id": "card-2"})


async def test_the_tag_write_is_staged_and_normalized_by_the_host():
    result = await run(
        flow({"op": "card.tags.set", "tags": ["  Noir ", "noir", "", "NOIR"]}),
        ctx={"character": {"id": "card-1", "name": "Mara"}},
    )
    assert result.effects.card_tags == ("card-1", ["Noir"])


async def test_the_tag_write_needs_a_card_in_context():
    with pytest.raises(FlowError, match="needs a character"):
        await run(flow({"op": "card.tags.set", "tags": ["noir"]}), ctx={})


async def test_the_tag_write_needs_both_grants():
    """Write-only would be a permission whose target the package cannot see."""
    with pytest.raises(FlowError, match="context.character.read"):
        await run(
            flow({"op": "card.tags.set", "tags": ["noir"]}),
            ctx={"character": {"id": "card-1"}},
            grants=frozenset({("card.tags.write", None)}),
        )


async def test_only_one_tag_write_per_invocation():
    with pytest.raises(FlowError, match="budget of 1 card tag writes"):
        await run(
            flow({"op": "card.tags.set", "tags": ["a"]}, {"op": "card.tags.set", "tags": ["b"]}),
            ctx={"character": {"id": "card-1"}},
        )


# ── the shared normalizer ───────────────────────────────────────────────────


def test_normalization_trims_dedupes_case_insensitively_and_caps():
    assert normalize_tags(["  Noir ", "noir", "NOIR"]) == ["Noir"]
    assert normalize_tags(["", "   ", None, 3]) == []
    assert len(normalize_tags([f"tag{i}" for i in range(MAX_TAGS_PER_CARD + 10)])) == MAX_TAGS_PER_CARD
    long = normalize_tags(["x" * (MAX_TAG_BYTES + 50)])
    assert len(long[0].encode("utf-8")) == MAX_TAG_BYTES


def test_normalization_clips_on_a_character_boundary():
    """Clipping mid-codepoint would store bytes that are not valid UTF-8."""
    clipped = normalize_tags(["é" * 100])[0]
    assert len(clipped.encode("utf-8")) <= MAX_TAG_BYTES
    assert clipped == "é" * (MAX_TAG_BYTES // 2)


def test_normalization_is_idempotent():
    """The property the "byte-identical stored tags" claim rests on."""
    messy = ["  Noir ", "noir", "", "x" * 200, *[f"t{i}" for i in range(40)]]
    once = normalize_tags(messy)
    assert normalize_tags(once) == once


def test_normalization_is_total_over_junk_input():
    """It runs on a write path that has always accepted whatever it was handed."""
    assert normalize_tags(None) == []
    assert normalize_tags("noir") == []
    assert normalize_tags([{"a": 1}, ["b"], 7, "ok"]) == ["ok"]
