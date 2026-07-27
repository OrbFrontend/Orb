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


# ── Conversation Map: the branch-tree reference package ─────────────────────
# The reason the conversation-tree resource, the shared branch action, and the
# `conversation-tree` component exist. It reads a core structure the API does
# not otherwise expose and mutates core state through a locked host action --
# a seam no other package exercises.


def conversation_map_manifest(**overrides: Any) -> dict[str, Any]:
    base = manifest(
        id="conversation-map",
        name="Conversation Map",
        description="A GitLens-like map of every branch in the conversation.",
        requires={
            "operations": ["conversation.branch.activate"],
            "components": ["card", "conversation-tree"],
        },
        permissions=[
            {"capability": "conversation.tree.read"},
            {"capability": "conversation.tree.previews"},
            {"capability": "conversation.branch.activate"},
            {"capability": "ui.contribute", "slot": "composer.menu"},
            {"capability": "ui.contribute", "slot": "workspace"},
        ],
        actions={
            "select": {
                "flow": "flows/select-branch.json",
                "label": "Go to this message",
                "input_schema": {
                    "type": "object",
                    "properties": {"message_id": {"type": "integer", "minimum": 1}},
                    "required": ["message_id"],
                    "additionalProperties": False,
                },
            }
        },
        views={"map": {"source": "ui/map.json", "label": "Conversation Map"}},
        commands=[
            {
                "id": "open-map",
                "label": "Conversation Map",
                "icon": "git-branch",
                "opens": "map",
                "when": {"exists": {"$ref": "host.active_conversation_id"}},
            }
        ],
        placements=[
            {"slot": "composer.menu", "command": "open-map"},
            {"slot": "workspace", "view": "map"},
        ],
    )
    base.update(overrides)
    return base


def select_branch_flow() -> dict[str, Any]:
    """One operation. Everything hard about branch activation is host-side.

    No model call and no HTTP request, which the flow parser enforces rather
    than trusts: activation holds the conversation stream lock, and an external
    round trip inside that window is the deadlock shape.
    """
    return {
        "flow_version": 1,
        "steps": [{"op": "conversation.branch.activate", "message_id": {"$ref": "input.message_id"}}],
    }


def conversation_map_view() -> dict[str, Any]:
    return {
        "view_version": 1,
        "data": {"tree": {"kind": "resource", "resource": "conversation.tree", "previews": True}},
        "root": {
            "component": "card",
            "title": "Branches",
            "children": [
                {
                    "component": "conversation-tree",
                    "nodes": {"$ref": "data.tree.nodes"},
                    "active_path": {"$ref": "data.tree.active_path"},
                    "select_action": "select",
                    "show_previews": True,
                    "empty_label": "This conversation has no messages yet.",
                }
            ],
        },
    }


def conversation_map_package(**overrides: Any) -> bytes:
    return orbext(
        {
            "orb-extension.json": conversation_map_manifest(**overrides),
            "flows/select-branch.json": select_branch_flow(),
            "ui/map.json": conversation_map_view(),
        }
    )


# ── Tag Librarian: the library-sweep reference package ──────────────────────
# The reason `list.join`, `list.intersect`, the paginated library resource,
# `card.tags.set`, and action-input card resolution exist. Its loop lives in
# the renderer, not in the flow: one action per card, each inside the ordinary
# per-invocation budget and each committing independently.

CLASSIFY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"card_id": {"type": "string", "maxLength": 64}},
    "required": ["card_id"],
    "additionalProperties": False,
}


def classify_flow() -> dict[str, Any]:
    """Classify one card against the user's vocabulary and write its tags.

    ``list.intersect`` is not decoration: a model asked to pick from a list will
    occasionally return something adjacent to it, and ``output_schema`` cannot
    express "one of the user's current tags" because schemas compile at install
    time while the vocabulary is runtime config. Without that step the package
    would launder invented tags into the library under the user's own
    vocabulary -- the exact mess it is installed to clean up.
    """
    return {
        "flow_version": 1,
        "steps": [
            {"id": "vocabulary", "op": "state.get", "scope": "config", "path": "vocabulary"},
            {"id": "vocabulary_text", "op": "list.join", "value": {"$ref": "steps.vocabulary"}, "separator": ", "},
            {
                "id": "proposed",
                "op": "model.structured",
                "lane": "agent",
                "prompt": {
                    "$template": (
                        "Choose every tag that applies to this character. Use only tags from the allowed list. "
                        "Return an empty array if none apply.\n\n"
                        "Allowed tags: {{steps.vocabulary_text}}\n\n"
                        "Name: {{ctx.character.name}}\n\n{{ctx.character.description}}"
                    )
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"tags": {"type": "array", "items": {"type": "string"}, "maxItems": 32}},
                    "required": ["tags"],
                    "additionalProperties": False,
                },
            },
            {
                "id": "allowed",
                "op": "list.intersect",
                "value": {"$ref": "steps.proposed.tags"},
                "allowed": {"$ref": "steps.vocabulary"},
            },
            {"op": "card.tags.set", "tags": {"$ref": "steps.allowed"}},
            {"op": "state.set", "scope": "character", "path": "tagged", "value": True},
            {"op": "return", "value": {"$ref": "steps.allowed"}},
        ],
    }


def tag_librarian_manifest(**overrides: Any) -> dict[str, Any]:
    base = manifest(
        id="tag-librarian",
        name="Tag Librarian",
        description="Classifies your characters against a tag vocabulary you maintain.",
        requires={
            "operations": [
                "state.get",
                "state.set",
                "list.join",
                "list.intersect",
                "model.structured",
                "card.tags.set",
                "return",
            ],
            "components": ["card", "library-sweep", "stack", "table", "text", "textarea"],
        },
        permissions=[
            {"capability": "context.character.read"},
            {"capability": "library.cards.read"},
            {"capability": "model.call", "lane": "agent"},
            {"capability": "state.read", "scope": "config"},
            {"capability": "state.write", "scope": "config"},
            {"capability": "state.read", "scope": "character"},
            {"capability": "state.write", "scope": "character"},
            {"capability": "card.tags.write"},
            {"capability": "ui.contribute", "slot": "tools"},
            {"capability": "ui.contribute", "slot": "workspace"},
        ],
        actions={"classify": {"flow": "flows/classify.json", "label": "Classify card", "input_schema": CLASSIFY_INPUT_SCHEMA}},
        views={
            "workspace": {"source": "ui/workspace.json", "label": "Tag Librarian"},
            "config": {"source": "ui/config.json", "label": "Vocabulary"},
        },
        commands=[{"id": "open-librarian", "label": "Tag Librarian", "icon": "tag", "opens": "workspace"}],
        placements=[
            {"slot": "tools", "command": "open-librarian"},
            {"slot": "workspace", "view": "workspace"},
        ],
    )
    base.update(overrides)
    return base


def tag_librarian_workspace_view() -> dict[str, Any]:
    return {
        "view_version": 1,
        "data": {"library": {"kind": "resource", "resource": "library.cards"}},
        "root": {
            "component": "card",
            "title": "Library",
            "children": [
                {
                    "component": "table",
                    "columns": [
                        {"key": "name", "label": "Character"},
                        {"key": "tags", "label": "Tags"},
                    ],
                    "rows": {"$ref": "data.library.cards"},
                    "empty_label": "No characters yet.",
                },
                # The loop lives in the host renderer. The package names an
                # action and the bookkeeping key that marks a card done; the
                # page size, cursor walk, concurrency, stop condition, and
                # progress display are all Orb's.
                {
                    "component": "library-sweep",
                    "action": "classify",
                    "label": "Classify unclassified cards",
                    "unclassified_key": "tagged",
                },
            ],
        },
    }


def tag_librarian_config_view() -> dict[str, Any]:
    """The `config` view convention: rendered in the manager's detail panel.

    Binds only ``config.*``. A config view that bound conversation or character
    state would be a placement-free write into per-entity data reachable from a
    panel the user opened to read a description, so the compiler refuses it.
    """
    return {
        "view_version": 1,
        "root": {
            "component": "stack",
            "children": [
                {"component": "text", "value": "One tag per line. Cards are classified against this list only."},
                # The flow reads this key with `list.join` and `list.intersect`,
                # so it has to be stored as an array. `value_kind: lines` is what
                # makes the box the user types in and the key the flow reads the
                # same slot -- a textarea bound to a second, text-shaped key
                # would save fine and classify nothing.
                {
                    "component": "textarea",
                    "bind": "config.vocabulary",
                    "label": "Vocabulary",
                    "rows": 6,
                    "value_kind": "lines",
                },
            ],
        },
    }


def tag_librarian_package(**overrides: Any) -> bytes:
    return orbext(
        {
            "orb-extension.json": tag_librarian_manifest(**overrides),
            "flows/classify.json": classify_flow(),
            "ui/workspace.json": tag_librarian_workspace_view(),
            "ui/config.json": tag_librarian_config_view(),
        }
    )
