"""The API 3 audit-detector contribution: gating, consent, and the DETECTOR context.

The Editor's mirror of ``test_writer_tool_contract``, and the properties that
matter are the same three: the slot is refused on an older API version, the
grant is mandatory, and the execution context admits exactly the operations a
flow running inside an unfinished turn may reach.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.extensions.compiler import (
    compile_package,
    derive_flow_requirements,
)
from backend.features.extensions.contracts import (
    Capability,
    ExtensionManifest,
    Flow,
    OpContext,
)
from backend.features.extensions.contracts.flow import check_context
from backend.features.extensions.errors import PackageValidationError
from backend.features.extensions.limits import MAX_AUDIT_DETECTORS
from backend.features.extensions.sources import ArchiveSource
from tests.extension_packages import (
    audit_detector_manifest,
    audit_detector_package,
    score_slop_flow,
)


def compile_bytes(data: bytes):
    with ArchiveSource(data) as source:
        return compile_package(source)


DETECTOR = {"id": "slop", "label": "Slop", "flow": "flows/score-slop.json"}


# ── version gating ───────────────────────────────────────────────────────────


def test_the_fixture_package_compiles_and_derives_its_grants():
    compiled = compile_bytes(audit_detector_package())
    assert compiled.unavailable == ()
    assert (Capability.AUDIT_DETECTOR_CONTRIBUTE.value, None) in compiled.requirements.permissions
    # The direction projection Phase 1 added is what a scorer reads to judge a
    # draft against the scene it was meant to write.
    assert (Capability.CONTEXT_READ.value, "direction") in compiled.requirements.permissions


@pytest.mark.parametrize("version", [1, 2])
def test_an_older_manifest_carrying_audit_detectors_is_rejected(version: int):
    """A v2 manifest must not acquire semantics its author never declared just
    because this build understands them."""
    with pytest.raises(ValidationError, match="requires extension_api 3"):
        ExtensionManifest.model_validate(audit_detector_manifest(extension_api=version))


def test_an_api_3_manifest_without_detectors_is_ordinary():
    manifest = ExtensionManifest.model_validate(
        audit_detector_manifest(extension_api=3, contributions={}, permissions=[], requires={})
    )
    assert manifest.contributions.audit_detectors == []


# ── consent ──────────────────────────────────────────────────────────────────


def test_contributing_a_detector_without_the_grant_is_refused():
    with pytest.raises(ValidationError, match="audit.detector.contribute"):
        ExtensionManifest.model_validate(
            audit_detector_manifest(permissions=[{"capability": "context.read", "field": "draft"}])
        )


def test_duplicate_detector_ids_are_refused():
    with pytest.raises(ValidationError, match="duplicate audit detector id"):
        ExtensionManifest.model_validate(audit_detector_manifest(contributions={"audit_detectors": [DETECTOR, DETECTOR]}))


def test_more_detectors_than_the_cap_are_refused():
    detectors = [{**DETECTOR, "id": f"d{i}"} for i in range(MAX_AUDIT_DETECTORS + 1)]
    with pytest.raises(ValidationError):
        ExtensionManifest.model_validate(audit_detector_manifest(contributions={"audit_detectors": detectors}))


def test_a_detector_flow_must_exist_in_the_package():
    with pytest.raises(PackageValidationError):
        compile_bytes(audit_detector_package(contributions={"audit_detectors": [{**DETECTOR, "flow": "flows/absent.json"}]}))


# ── the DETECTOR execution context ───────────────────────────────────────────


@pytest.mark.parametrize(
    "step",
    [
        {"op": "draft.replace", "value": "x"},
        {"op": "context.append", "targets": ["writer"], "label": "L", "text": "x"},
        {"op": "artifact.emit", "filename": "a.txt", "mime": "text/plain", "data": "x"},
        {"op": "card.tags.set", "tags": []},
        {"op": "conversation.branch.activate", "message_id": 1},
        {"op": "ui.toast", "text": "x"},
        {"op": "ui.invalidate", "view": "v"},
        {"op": "ui.status", "text": "x"},
    ],
)
def test_a_denied_operation_is_refused_in_the_detector_context(step):
    """A detector runs inside an unfinished turn with no user click and no
    assistant row: no UI surface, no draft to replace (the Editor owns the
    rewrite), nothing to attach to."""
    flow = Flow.model_validate({"flow_version": 1, "steps": [step]})
    assert check_context(flow, OpContext.DETECTOR)


@pytest.mark.parametrize(
    "step",
    [
        {"op": "state.get", "scope": "conversation", "path": "x"},
        {"op": "state.set", "scope": "character", "path": "x", "value": 1},
        {"op": "random.integer", "minimum": 1, "maximum": 6},
        {"op": "model.text", "lane": "agent", "prompt": "hi"},
        {"op": "http.request", "method": "GET", "url": "https://example.invalid/x"},
        {"op": "math.clamp", "value": 1, "minimum": 0, "maximum": 2},
        {"op": "return", "value": 1},
    ],
)
def test_an_allowed_operation_passes_the_detector_context(step):
    """EXTERNAL_CONTEXTS, not IMPURE_CONTEXTS: model calls, state, and HTTP are
    the whole point of a classifier-backed detector."""
    flow = Flow.model_validate({"flow_version": 1, "steps": [step]})
    assert check_context(flow, OpContext.DETECTOR) == []


def test_message_scoped_state_is_refused_at_compile_time():
    flow = Flow.model_validate({"flow_version": 1, "steps": [{"op": "state.set", "scope": "message", "path": "x", "value": 1}]})
    problems = check_context(flow, OpContext.DETECTOR)
    assert any("scope 'message'" in problem for problem in problems)
    # Still legal where a message exists.
    assert check_context(flow, OpContext.POST_OBSERVE) == []


def test_the_scorer_flow_derives_the_model_and_context_grants():
    flow = Flow.model_validate(score_slop_flow())
    assert derive_flow_requirements(flow) == {
        (Capability.MODEL_CALL.value, "agent"),
        (Capability.CONTEXT_READ.value, "draft"),
        (Capability.CONTEXT_READ.value, "direction"),
    }


# ── the fingerprint ──────────────────────────────────────────────────────────


def test_renaming_a_detector_label_changes_the_contract_fingerprint():
    """The label becomes the report's section heading, so it is package-authored
    text the user consented to -- changing it is an inspected update."""
    before = compile_bytes(audit_detector_package()).contract_fingerprint
    after = compile_bytes(
        audit_detector_package(
            contributions={"audit_detectors": [{"id": "slop", "label": "Renamed", "flow": "flows/score-slop.json"}]}
        )
    ).contract_fingerprint
    assert before != after
