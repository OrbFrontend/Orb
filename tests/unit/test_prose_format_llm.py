"""Unit tests for the prose_format_llm workflow's pure logic + loop orchestration.

No LLM, no DB: the loop is exercised with stub async-generator judge/enforce
factories, and the validators/patcher/state helpers are table-tested directly.
The transport-interop cases at the end are pure as well -- they push this
workflow's own registered tool schemas through the functions the structured
forced-call path is built from, without a wire.
"""

from __future__ import annotations

import json

import pytest

from backend.inference.client import parse_tool_calls, strictify_schema
from backend.inference.text_completion import forced_schema, forced_tool_message
from backend.workflows.prose_format_llm import ANALYZE_TOOL, PATCH_TOOL, REPORT_TOOL
from backend.workflows.prose_format_llm.loop import run_enforcement_loop
from backend.workflows.prose_format_llm.patching import apply_patches
from backend.workflows.prose_format_llm.statedoc import filled_elements, is_armed, seed
from backend.workflows.prose_format_llm.violations import (
    clean_analyzer_records,
    validate_violations,
)

# --- validate_violations ---


def test_validate_violations_keeps_valid_and_counts():
    raw = [{"excerpt": "a", "category": "narration"}, {"excerpt": "b", "category": "speech"}]
    out = validate_violations(raw, "a b", {"narration", "speech"})
    assert out == raw


def test_validate_violations_drops_absent_excerpt():
    out = validate_violations([{"excerpt": "zzz", "category": "narration"}], "hello", {"narration"})
    assert out == []


def test_validate_violations_drops_unknown_category():
    out = validate_violations([{"excerpt": "hello", "category": "foo"}], "hello", {"narration"})
    assert out == []


def test_validate_violations_dedups():
    raw = [{"excerpt": "hi", "category": "narration"}, {"excerpt": "hi", "category": "narration"}]
    assert validate_violations(raw, "hi there", {"narration"}) == [{"excerpt": "hi", "category": "narration"}]


def test_validate_violations_tolerates_garbage():
    assert validate_violations(None, "x", {"narration"}) == []
    assert validate_violations(["nope", 1, {}], "x", {"narration"}) == []


# --- apply_patches ---


def test_apply_patches_single_match():
    draft, errors = apply_patches("hello world", [{"search": "world", "replace": "there"}])
    assert draft == "hello there"
    assert errors == []


def test_apply_patches_noop_and_empty_skip_silently():
    draft, errors = apply_patches("ab", [{"search": "a", "replace": "a"}, {"search": "", "replace": "x"}])
    assert draft == "ab"
    assert errors == []


def test_apply_patches_not_found_errors():
    draft, errors = apply_patches("hello", [{"search": "zzz", "replace": "x"}])
    assert draft == "hello"
    assert len(errors) == 1


def test_apply_patches_ambiguous_errors():
    draft, errors = apply_patches("a a", [{"search": "a", "replace": "b"}])
    assert draft == "a a"
    assert len(errors) == 1


def test_apply_patches_mixed_outcomes():
    draft, errors = apply_patches(
        "keep world",
        [{"search": "world", "replace": "there"}, {"search": "missing", "replace": "x"}],
    )
    assert draft == "keep there"
    assert len(errors) == 1


def test_apply_patches_tolerates_garbage():
    assert apply_patches("x", None) == ("x", [])
    draft, errors = apply_patches("x", ["nope", {"search": 1, "replace": "y"}])
    assert draft == "x"
    assert len(errors) == 2


# --- statedoc ---


def test_seed_is_unarmed():
    st = seed()
    assert st["values"] == {}
    assert st["auto_analyzed"] is False
    assert is_armed(st) is False
    assert filled_elements(st) == {}


def test_filled_elements_partial():
    st = {"values": {"narration": "asterisks", "speech": "  ", "x": ""}}
    assert filled_elements(st) == {"narration": "asterisks"}
    assert is_armed(st) is True


def test_filled_elements_ignores_non_string():
    assert filled_elements({"values": {"narration": 123}}) == {}
    assert is_armed(None) is False


# --- clean_analyzer_records ---


def test_clean_analyzer_records_keeps_valid_schema_strings():
    raw = [{"category": "narration", "denotation": "asterisks"}]
    assert clean_analyzer_records(raw, {"narration", "speech"}) == {"narration": "asterisks"}


def test_clean_analyzer_records_drops_unknown_empty_and_nonstring():
    raw = [
        {"category": "foo", "denotation": "x"},
        {"category": "narration", "denotation": "  "},
        {"category": "speech", "denotation": 5},
        "garbage",
    ]
    assert clean_analyzer_records(raw, {"narration", "speech"}) == {}
    assert clean_analyzer_records(None, {"narration"}) == {}


# --- run_enforcement_loop ---

_V = [{"excerpt": "x", "category": "narration"}]
_VV = [{"excerpt": "x", "category": "narration"}, {"excerpt": "y", "category": "speech"}]


def _stub_judge(results, log):
    """Async-gen judge factory yielding the i-th canned (already-validated) result."""
    idx = {"i": 0}

    async def judge_fn(draft):
        i = idx["i"]
        idx["i"] += 1
        log.append("judge")
        yield {"type": "result", "violations": list(results[i]) if i < len(results) else []}

    return judge_fn


def _stub_enforce(log, patches=None):
    async def enforce_fn(draft, violations):
        log.append("enforce")
        yield {"type": "result", "patches": [{"search": "x", "replace": "z"}] if patches is None else patches}

    return enforce_fn


def _apply_changes(draft, patches):
    return draft + "#", []


def _apply_with_errors(draft, patches):
    return draft, ["patch 0: search not found"]


async def _drain(gen):
    events, final = [], None
    async for ev in gen:
        if ev.get("type") == "loop_done":
            final = ev["draft"]
        else:
            events.append(ev)
    return events, final


async def test_loop_early_exit_when_clean():
    log: list[str] = []
    _, final = await _drain(
        run_enforcement_loop("draft", 1, _stub_judge([[]], log), _stub_enforce(log), _apply_changes, lambda: False)
    )
    assert final == "draft"
    assert log == ["judge"]


async def test_loop_n_zero_is_diagnostic():
    log: list[str] = []
    events, final = await _drain(
        run_enforcement_loop("draft", 0, _stub_judge([_V], log), _stub_enforce(log), _apply_changes, lambda: False)
    )
    assert final == "draft"
    assert log == ["judge"]
    assert any(e["data"]["pass"].endswith(":judge") for e in events)


async def test_loop_converges_with_call_count():
    log: list[str] = []
    _, final = await _drain(
        run_enforcement_loop("draft", 1, _stub_judge([_V, []], log), _stub_enforce(log), _apply_changes, lambda: False)
    )
    assert final == "draft#"
    # 1 + 2N with N=1: initial judge, enforce, re-judge.
    assert log == ["judge", "enforce", "judge"]


async def test_loop_no_progress_break():
    log: list[str] = []
    _, final = await _drain(
        run_enforcement_loop("draft", 3, _stub_judge([_VV, _VV], log), _stub_enforce(log), _apply_changes, lambda: False)
    )
    assert log == ["judge", "enforce", "judge"]
    assert final == "draft#"


async def test_loop_cap_break():
    log: list[str] = []
    await _drain(
        run_enforcement_loop("draft", 1, _stub_judge([_VV, _V], log), _stub_enforce(log), _apply_changes, lambda: False)
    )
    # N=1 caps it after one enforce even though violations remain.
    assert log == ["judge", "enforce", "judge"]


async def test_loop_abort_break():
    log: list[str] = []
    _, final = await _drain(
        run_enforcement_loop("draft", 2, _stub_judge([_V], log), _stub_enforce(log), _apply_changes, lambda: True)
    )
    assert log == ["judge"]
    assert final == "draft"


async def test_loop_surfaces_apply_errors():
    log: list[str] = []
    events, _ = await _drain(
        run_enforcement_loop("draft", 1, _stub_judge([_V, []], log), _stub_enforce(log), _apply_with_errors, lambda: False)
    )
    assert any(e["data"]["pass"].endswith(":enforce") and "skipped" in e["data"]["delta"] for e in events)


# --- structured-output transport interop ---

# Every path in this workflow is a forced tool call, so on an endpoint whose
# profile opts into structured output the transport withholds `tools` entirely
# and rebuilds the call from `response_format` plus a synthesized message. These
# cases pin that our three schemas survive that rewrite; the transport's own
# behavior is covered in tests/unit/test_structured_tool_calls.py.
_INTEROP_CASES = [
    (ANALYZE_TOOL, {"records": [{"category": "narration", "denotation": "asterisks, past, third"}]}),
    (REPORT_TOOL, {"violations": [{"excerpt": "He ran.", "category": "narration"}]}),
    (PATCH_TOOL, {"patches": [{"search": "He ran.", "replace": "*He ran.*"}]}),
]
_INTEROP_IDS = [spec.name for spec, _ in _INTEROP_CASES]


@pytest.mark.parametrize(("spec", "args"), _INTEROP_CASES, ids=_INTEROP_IDS)
def test_tool_schema_survives_strict_rewrite(spec, args):
    schema = forced_schema([spec.schema], spec.choice)
    # A ToolSpec whose choice does not name its own schema yields None here, and
    # the transport then sends no response_format at all -- the forced call would
    # quietly degrade to an unforced one.
    assert schema is not None
    strict = strictify_schema(schema)

    assert strict["additionalProperties"] is False
    (array_key,) = strict["required"]

    items = strict["properties"][array_key]["items"]
    assert items["additionalProperties"] is False
    # `_array_tool` marks every item property required, so strict mode must not
    # widen any of them to `["string", "null"]`. A nullable leaf would decode as
    # None and be dropped by the string guards in violations.py / patching.py --
    # a silently empty result rather than a visible failure.
    assert set(items["required"]) == set(items["properties"])
    assert all(prop["type"] == "string" for prop in items["properties"].values())
    # Keeps the sample payloads honest if the item properties ever change.
    assert set(args[array_key][0]) == set(items["properties"])


@pytest.mark.parametrize(("spec", "args"), _INTEROP_CASES, ids=_INTEROP_IDS)
def test_structured_content_round_trips_to_args(spec, args):
    """The transport synthesizes a tool-call message from grammar-constrained
    content; the arguments the workflow reads must come back byte-identical."""
    message = forced_tool_message(spec.name, json.dumps(args))
    assert parse_tool_calls(message) == [{"name": spec.name, "arguments": args}]
