"""Builders for ``.orbext`` test packages, shared by unit and integration tests.

Lives at the top of the ``tests`` package rather than beside either suite
because both need the *same* archives: a unit test asserting what the compiler
derives and an integration test asserting what the install route does with it
must not be reasoning about two subtly different manifests.

:func:`manifest` produces the minimal valid package -- a metadata-only
extension with no hooks, actions, or views. Everything else is a keyword
override, so a test that cares about permissions says only what it changes and
a reader can see the whole difference in one call.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

DEFAULT_ID = "scene-meter"

# 1x1 transparent PNG. Real leading bytes, because the asset checker verifies
# the signature -- a placeholder of b"png" would pass the extension check and
# fail the one that matters.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415478da6364f8cf500f00038601805a347d6b0000000049454e44"
    "ae426082"
)


def manifest(**overrides: Any) -> dict[str, Any]:
    """A valid metadata-only manifest, plus whatever the caller overrides."""
    base: dict[str, Any] = {
        "extension_api": 1,
        "id": DEFAULT_ID,
        "name": "Scene Meter",
        "version": "1.0.0",
        "author": "Example Author",
        "description": "Tracks and displays scene tension.",
    }
    base.update(overrides)
    return base


def scoring_flow() -> dict[str, Any]:
    """A post-pipeline flow that reads the draft, calls a model, writes state."""
    return {
        "flow_version": 1,
        "steps": [
            {
                "id": "score",
                "op": "model.structured",
                "lane": "agent",
                "prompt": {"$template": "Rate scene tension from 0 to 100.\n\n{{ctx.draft}}"},
                "output_schema": {
                    "type": "object",
                    "properties": {"tension": {"type": "integer", "minimum": 0, "maximum": 100}},
                    "required": ["tension"],
                    "additionalProperties": False,
                },
            },
            {"op": "state.set", "scope": "conversation", "path": "tension", "value": {"$ref": "steps.score.tension"}},
            {"op": "ui.invalidate", "view": "inspector"},
        ],
    }


def reset_flow() -> dict[str, Any]:
    """An action flow: clamp the requested value into range and store it.

    Deliberately pure -- no model, no network -- so a test can assert the
    action transaction (validate input, lock, stage, commit, envelope) without
    a completion in the middle of it.
    """
    return {
        "flow_version": 1,
        "steps": [
            {"id": "clamped", "op": "math.clamp", "value": {"$ref": "input.tension"}, "minimum": 0, "maximum": 100},
            {"op": "state.set", "scope": "conversation", "path": "tension", "value": {"$ref": "steps.clamped"}},
            {"op": "ui.invalidate", "view": "inspector"},
            {"op": "return", "value": {"tension": {"$ref": "steps.clamped"}}},
        ],
    }


def context_flow() -> dict[str, Any]:
    """A pre-pipeline flow: read stored tension and add it to both prompts."""
    return {
        "flow_version": 1,
        "steps": [
            {"id": "tension", "op": "state.get", "scope": "conversation", "path": "tension"},
            {
                "op": "context.append",
                "when": {"exists": {"$ref": "steps.tension"}},
                "targets": ["director", "writer"],
                "label": "Scene tension",
                "text": {"$template": "Current scene tension is {{steps.tension}} out of 100."},
            },
        ],
    }


def meter_view() -> dict[str, Any]:
    return {
        "view_version": 1,
        "root": {
            "component": "card",
            "title": "Scene Meter",
            "children": [{"component": "meter", "value": {"$ref": "ctx.draft"}, "minimum": 0, "maximum": 100}],
        },
    }


def full_manifest(**overrides: Any) -> dict[str, Any]:
    """A package that exercises a hook, a view, a placement, and five grants."""
    base = manifest(
        requires={
            "operations": ["model.structured", "state.set", "ui.invalidate"],
            "components": ["card", "meter"],
        },
        permissions=[
            {"capability": "context.draft.read"},
            {"capability": "model.call", "lane": "agent"},
            {"capability": "state.read", "scope": "conversation"},
            {"capability": "state.write", "scope": "conversation"},
            {"capability": "ui.contribute", "slot": "inspector"},
        ],
        hooks={"post_pipeline": {"flow": "flows/score-scene.json", "stage": "observe"}},
        views={"inspector": {"source": "ui/inspector.json"}},
        placements=[{"slot": "inspector", "view": "inspector"}],
    )
    base.update(overrides)
    return base


RESET_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"tension": {"type": "integer", "minimum": -1000, "maximum": 1000}},
    "required": ["tension"],
    "additionalProperties": False,
}


def scene_meter_manifest(**overrides: Any) -> dict[str, Any]:
    """The Scene Meter reference package: a hook, an action, and a view.

    The Phase 2 fixture. Unlike :func:`full_manifest` it exercises the whole
    executable surface -- a pre-pipeline context block, a post-pipeline
    structured model call writing conversation state, and a named action a
    button dispatches -- so a test can assert real invocation behavior rather
    than only what the compiler derived.
    """
    base = manifest(
        requires={
            "operations": [
                "state.get",
                "state.set",
                "context.append",
                "model.structured",
                "math.clamp",
                "ui.invalidate",
                "return",
            ],
            "components": ["card", "meter"],
        },
        permissions=[
            {"capability": "context.draft.read"},
            {"capability": "model.call", "lane": "agent"},
            {"capability": "state.read", "scope": "conversation"},
            {"capability": "state.write", "scope": "conversation"},
            {"capability": "prompt.context.append", "targets": ["director", "writer"]},
            {"capability": "ui.contribute", "slot": "inspector"},
        ],
        hooks={
            "pre_pipeline": {"flow": "flows/inject-tension.json"},
            "post_pipeline": {"flow": "flows/score-scene.json", "stage": "observe"},
        },
        actions={"reset": {"flow": "flows/reset.json", "label": "Reset tension", "input_schema": RESET_INPUT_SCHEMA}},
        views={"inspector": {"source": "ui/inspector.json"}},
        placements=[{"slot": "inspector", "view": "inspector"}],
    )
    base.update(overrides)
    return base


def scene_meter_package(**overrides: Any) -> bytes:
    """The Scene Meter reference package as one archive."""
    return orbext(
        {
            "orb-extension.json": scene_meter_manifest(**overrides),
            "flows/inject-tension.json": context_flow(),
            "flows/score-scene.json": scoring_flow(),
            "flows/reset.json": reset_flow(),
            "ui/inspector.json": meter_view(),
        }
    )


def orbext(files: dict[str, Any], *, root: str = "") -> bytes:
    """Zip *files* into ``.orbext`` bytes.

    Values that are ``bytes`` are written verbatim; anything else is JSON
    encoded. *root* nests everything under one directory, which is what
    ``zip -r pkg.orbext my-extension/`` produces and what the reader strips.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            name = f"{root}/{path}" if root else path
            archive.writestr(name, content if isinstance(content, bytes) else json.dumps(content))
    return buffer.getvalue()


def metadata_package(**overrides: Any) -> bytes:
    """The simplest installable package: a manifest and nothing else."""
    return orbext({"orb-extension.json": manifest(**overrides)})


def full_package(**overrides: Any) -> bytes:
    """The hook/view/placement package, as one archive."""
    return orbext(
        {
            "orb-extension.json": full_manifest(**overrides),
            "flows/score-scene.json": scoring_flow(),
            "ui/inspector.json": meter_view(),
        }
    )
