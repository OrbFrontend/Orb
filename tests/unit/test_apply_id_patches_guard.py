"""Guards on apply_id_patches — the id-anchored replacement for search/replace.

The error strings are fed back to the model verbatim as the tool result, so
they are part of the contract and asserted as such.
"""

from __future__ import annotations

import pytest

from backend.analysis import PatchError, PatchErrorKind, Target, apply_id_patches

DRAFT = "Alpha one. Beta two. Gamma three."


@pytest.fixture
def targets() -> list[Target]:
    return [
        Target(tid=1, span="Alpha one.", start=0, end=10),
        Target(tid=2, span="Beta two.", start=11, end=20),
        Target(tid=3, span="Gamma three.", start=21, end=33),
    ]


def test_applies_by_id(targets):
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 2, "replace": "Delta four."}])
    assert out == "Alpha one. Delta four. Gamma three."
    assert errors == []


def test_multiple_patches_splice_back_to_front(targets):
    out, errors = apply_id_patches(
        DRAFT,
        targets,
        [
            {"id": 1, "replace": "A much longer opening sentence."},
            {"id": 3, "replace": "C."},
        ],
    )
    assert out == "A much longer opening sentence. Beta two. C."
    assert errors == []


def test_patch_order_does_not_matter(targets):
    forward, _ = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": "X."}, {"id": 3, "replace": "Y."}])
    reverse, _ = apply_id_patches(DRAFT, targets, [{"id": 3, "replace": "Y."}, {"id": 1, "replace": "X."}])
    assert forward == reverse == "X. Beta two. Y."


def test_empty_replace_deletes_the_span(targets):
    # The span never included its separator, so a bare splice would strand one.
    # Healing closes the seam — see tests/unit/test_patch_healing.py.
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": ""}])
    assert out == "Beta two. Gamma three."
    assert errors == []


# ── Error vocabulary ──────────────────────────────────────────────────────────


def test_non_dict_patch_element_reported(targets):
    # The guard exists for runtime input the type system forbids, so the bad
    # element is deliberately the wrong type here.
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": "X."}, "junk"])
    assert out == "X. Beta two. Gamma three."
    assert errors == ["Error: patch 1 is not an object with `id` and `replace`."]


def test_unknown_id_reported_with_the_valid_range(targets):
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 9, "replace": "X."}])
    assert out == DRAFT
    assert errors == ["Error: no finding with id 9 in the report. Valid ids: 1-3."]


def test_missing_id_reported_as_non_integer(targets):
    _, errors = apply_id_patches(DRAFT, targets, [{"replace": "X."}])
    assert errors == ["Error: patch 0 has a non-integer id (None). Valid ids: 1-3."]


@pytest.mark.parametrize("raw_id", ["2", " 2 ", 2.0])
def test_stringy_and_float_ids_are_coerced(targets, raw_id):
    # The forced-decoding grammar holds ids to integers, but the chat-transport
    # recovery chain can hand back "2" or 2.0; both name finding 2 unambiguously.
    out, errors = apply_id_patches(DRAFT, targets, [{"id": raw_id, "replace": "Delta four."}])
    assert out == "Alpha one. Delta four. Gamma three."
    assert errors == []


@pytest.mark.parametrize("raw_id", [True, 1.5, "one", None, [1]])
def test_ids_that_name_nothing_are_rejected(targets, raw_id):
    # `True` is an int in Python but never a finding id, so it must not read as 1.
    out, errors = apply_id_patches(DRAFT, targets, [{"id": raw_id, "replace": "X."}])
    assert out == DRAFT
    assert errors == [f"Error: patch 0 has a non-integer id ({raw_id!r}). Valid ids: 1-3."]


def test_duplicate_id_applies_once_and_reports(targets):
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": "X."}, {"id": 1, "replace": "Y."}])
    assert out == "X. Beta two. Gamma three."
    assert errors == ["Error: id 1 was patched more than once. Emit exactly one patch per finding."]


def test_missing_replace_reported(targets):
    _, errors = apply_id_patches(DRAFT, targets, [{"id": 1}])
    assert errors == ["Error: the patch for id 1 has no `replace` text."]


@pytest.mark.parametrize("bad", [42, {"a": 1}, ["x", "y"], True])
def test_non_text_replace_is_rejected_not_stringified(targets, bad):
    # Unlike `id`, a non-string `replace` names nothing recoverable: coercing it
    # would splice `['x', 'y']` into the user's prose with no error anywhere.
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": bad}])
    assert out == DRAFT
    assert errors == [f"Error: the patch for id 1 has a non-text `replace` ({type(bad).__name__}). Send a string."]


def test_one_error_per_patch_that_did_not_apply(targets):
    # Document mode counts applications as len(patches) - len(errors), which only
    # holds if every rejected element contributes exactly one error.
    patches = [{"id": 1, "replace": "X."}, "junk", {"id": 9, "replace": "Y."}, {"id": 3, "replace": "Z."}]
    out, errors = apply_id_patches(DRAFT, targets, patches)
    assert out == "X. Beta two. Z."
    assert len(patches) - len(errors) == 2


def test_no_op_replace_reported(targets):
    out, errors = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": "Alpha one."}])
    assert out == DRAFT
    assert errors == ["Error: the patch for id 1 is a no-op — `replace` repeats the flagged text unchanged."]


def test_empty_target_list_names_no_range():
    _, errors = apply_id_patches(DRAFT, [], [{"id": 1, "replace": "X."}])
    assert errors == ["Error: no finding with id 1 in the report. Valid ids: (none — the report has no numbered findings)."]


# ── Error metadata ────────────────────────────────────────────────────────────
#
# The message is written for the model; the kind and id are written for the loop
# around it. The editor tells a rejected real target apart from a junk id on
# these, so they are as much a contract as the strings above.


def test_errors_are_plain_strings_to_every_existing_caller(targets):
    # A str subclass, so joining, comparing and counting are unchanged — the
    # tool-result text and the `len(patches) - len(errors)` count both rely on
    # that staying true.
    _, errors = apply_id_patches(DRAFT, targets, [{"id": 9, "replace": "X."}])
    assert isinstance(errors[0], str)
    assert errors == ["Error: no finding with id 9 in the report. Valid ids: 1-3."]
    assert "\n".join(errors) == errors[0]


@pytest.mark.parametrize(
    ("patch", "kind", "tid"),
    [
        ("junk", PatchErrorKind.MALFORMED, None),
        ({"replace": "X."}, PatchErrorKind.MALFORMED, None),
        ({"id": 1}, PatchErrorKind.MALFORMED, 1),
        ({"id": 1, "replace": 42}, PatchErrorKind.MALFORMED, 1),
        ({"id": 9, "replace": "X."}, PatchErrorKind.UNKNOWN_ID, 9),
        ({"id": 1, "replace": "Alpha one."}, PatchErrorKind.NO_OP, 1),
    ],
)
def test_each_failure_carries_its_kind_and_finding(targets, patch, kind, tid):
    _, errors = apply_id_patches(DRAFT, targets, [patch])
    assert (errors[0].kind, errors[0].tid) == (kind, tid)


def test_a_duplicate_id_is_its_own_kind(targets):
    _, errors = apply_id_patches(DRAFT, targets, [{"id": 1, "replace": "X."}, {"id": 1, "replace": "Y."}])
    assert (errors[0].kind, errors[0].tid) == (PatchErrorKind.DUPLICATE_ID, 1)


def test_healing_and_guarding_are_distinguishable():
    # Both leave a target unrepaired, but only one of them means the model
    # copied text it was never given: the editor replays that one.
    draft = '"I\'m bored." She murmured.'
    span = "She murmured."
    start = draft.index(span)
    restated = [Target(tid=1, span=span, start=start, end=start + len(span))]
    _, errors = apply_id_patches(draft, restated, [{"id": 1, "replace": '"I\'m bored."'}])
    assert errors[0].kind == PatchErrorKind.RESTATED_CONTEXT

    draft = "Don't touch the latch. The room was very quiet. She waited."
    span = "The room was very quiet."
    start = draft.index(span)
    cloned = [Target(tid=1, span=span, start=start, end=start + len(span))]
    _, errors = apply_id_patches(draft, cloned, [{"id": 1, "replace": "Don't touch the latch, she thought."}])
    assert (errors[0].kind, errors[0].tid) == (PatchErrorKind.PROTECTED_SEQUENCE, 1)


def test_a_patch_error_can_be_built_directly():
    error = PatchError("Error: something.", tid=3, kind=PatchErrorKind.NO_OP)
    assert error == "Error: something."
    assert (error.tid, error.kind) == (3, PatchErrorKind.NO_OP)
