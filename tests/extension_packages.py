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
            {"capability": "context.read", "field": "draft"},
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
            {"capability": "context.read", "field": "draft"},
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


def fragment_meter_reduce_flow() -> dict[str, Any]:
    """Pure Phase 5 reducer: add the bounded delta, clamp, and retain why."""
    return {
        "flow_version": 1,
        "steps": [
            {
                "id": "advanced",
                "op": "math.add",
                "a": {"$ref": "fragment.previous.value"},
                "b": {"$ref": "fragment.director.delta"},
            },
            {
                "id": "clamped",
                "op": "math.clamp",
                "value": {"$ref": "steps.advanced"},
                "minimum": {"$ref": "fragment.config.minimum"},
                "maximum": {"$ref": "fragment.config.maximum"},
            },
            {
                "op": "return",
                "value": {
                    "value": {"$ref": "steps.clamped"},
                    "reason": {"$ref": "fragment.director.reason"},
                },
            },
        ],
    }


def fragment_meter_config_view() -> dict[str, Any]:
    return {
        "view_version": 1,
        "root": {
            "component": "card",
            "title": "Meter settings",
            "children": [
                {"component": "number-input", "bind": "config.minimum", "label": "Minimum"},
                {"component": "number-input", "bind": "config.maximum", "label": "Maximum"},
                {"component": "number-input", "bind": "config.initial", "label": "Initial value"},
                {
                    "component": "number-input",
                    "bind": "config.max_delta",
                    "label": "Maximum change per turn",
                    "minimum": 1,
                    "maximum": 100,
                },
            ],
        },
    }


def fragment_meter_value_view() -> dict[str, Any]:
    return {
        "view_version": 1,
        "root": {
            "component": "meter",
            "label": "Current value",
            "value": {"$ref": "data.fragment.current.value"},
            "minimum": {"$ref": "config.minimum"},
            "maximum": {"$ref": "config.maximum"},
        },
    }


def fragment_meter_manifest(**overrides: Any) -> dict[str, Any]:
    """The Phase 5 reference package: one configured progressive type."""
    base = manifest(
        requires={
            "operations": ["math.add", "math.clamp", "return"],
            "components": ["card", "meter", "number-input"],
        },
        permissions=[{"capability": "fragment_type.contribute"}],
        contributions={
            "fragment_types": [
                {
                    "id": "meter",
                    "label": "Meter",
                    "description": "A bounded numeric state that changes by a Director-selected delta.",
                    "storage": "assistant_progressive",
                    "config_schema": {
                        "type": "object",
                        "properties": {
                            "minimum": {"type": "integer", "default": 0},
                            "maximum": {"type": "integer", "default": 100},
                            "initial": {"type": "integer", "default": 50},
                            "max_delta": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                        },
                        "required": ["minimum", "maximum", "initial", "max_delta"],
                        "additionalProperties": False,
                    },
                    "director_schema": {
                        "type": "object",
                        "properties": {
                            "delta": {
                                "type": "integer",
                                "minimum": {"$neg_config": "max_delta"},
                                "maximum": {"$config": "max_delta"},
                            },
                            "reason": {"type": "string", "maxLength": 160},
                        },
                        "required": ["delta", "reason"],
                        "additionalProperties": False,
                    },
                    "prior_context": {"$template": "{{fragment.injection_label}} is currently {{fragment.previous.value}}."},
                    "reduce_flow": "flows/reduce-meter.json",
                    "writer_context": {
                        "$template": "{{fragment.injection_label}}: {{fragment.previous.value}} → "
                        "{{fragment.current.value}} ({{fragment.current.reason}})"
                    },
                    "config_view": "ui/meter-config.json",
                    "value_view": "ui/meter-value.json",
                }
            ]
        },
    )
    base.update(overrides)
    return base


def fragment_meter_package(**overrides: Any) -> bytes:
    return orbext(
        {
            "orb-extension.json": fragment_meter_manifest(**overrides),
            "flows/reduce-meter.json": fragment_meter_reduce_flow(),
            "ui/meter-config.json": fragment_meter_config_view(),
            "ui/meter-value.json": fragment_meter_value_view(),
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
            {"capability": "conversation.tree.read", "field": "structure"},
            {"capability": "conversation.tree.read", "field": "preview"},
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
            {"capability": "context.read", "field": "character"},
            {"capability": "library.cards.read"},
            {"capability": "model.call", "lane": "agent"},
            {"capability": "state.read", "scope": "config"},
            {"capability": "state.write", "scope": "config"},
            {"capability": "state.read", "scope": "character"},
            {"capability": "state.write", "scope": "character"},
            {"capability": "card.write", "field": "tags"},
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


# ── API Artifact: the network/secret/artifact reference package ─────────────
# The reason `http.request`, the pinned-address client, write-only secrets, and
# `artifact.emit` exist. It is the only package that leaves the machine: it
# sends a bounded request to one consented origin with a secret in a header,
# receives bytes it never decodes, and attaches them to the message being
# written. Its regenerate/reroll pair is what makes those bytes recoverable
# after eviction without Orb ever re-running an old revision.

API_ARTIFACT_ORIGIN = "https://api.example.invalid"

FETCH_RECOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"prompt": {"type": "string", "maxLength": 2000}},
    "required": ["prompt"],
    "additionalProperties": False,
}


def _render_request(prompt_value: dict[str, Any]) -> list[dict[str, Any]]:
    """The two steps every API Artifact flow shares: request, then attach.

    Factored out because regenerate and reroll must send the *same* request the
    original did -- a recovery path that differed from the production path would
    return bytes the user never asked for, and the difference would only show up
    after an eviction.
    """
    return [
        {
            "id": "render",
            "op": "http.request",
            "method": "POST",
            "url": f"{API_ARTIFACT_ORIGIN}/v1/render",
            # A list of parts, not a template: a template's output is an
            # ordinary flow value, and a secret must never become one.
            "headers": {"Authorization": ["Bearer ", {"$secret": "api_key"}]},
            "body": {"prompt": prompt_value},
            "response": "bytes",
        },
        {
            "op": "artifact.emit",
            "filename": "render.png",
            "mime": "image/png",
            "data": {"$ref": "steps.render.body"},
            "annotation": "Rendered by API Artifact",
            # Recorded so a later regenerate can reproduce this exact call. The
            # host adds the producing revision's id, version, and digest beside
            # it; the package supplies only what its own flow needs back.
            "recovery": {"prompt": prompt_value},
        },
    ]


def api_artifact_hook_flow() -> dict[str, Any]:
    """Post-pipeline: render the finished reply and attach the result."""
    return {
        "flow_version": 1,
        "steps": [
            {"op": "ui.status", "text": "Rendering..."},
            *_render_request({"$ref": "ctx.draft"}),
        ],
    }


def api_artifact_recovery_flow() -> dict[str, Any]:
    """Regenerate and reroll: the same request from the stored parameters.

    One file bound to both hooks. They differ only in the seed the host hands
    them -- regenerate replays the original, reroll gets a fresh one -- which is
    the framework's distinction, not the package's, so the package has no reason
    to hold two copies of the same steps.
    """
    return {
        "flow_version": 1,
        "steps": _render_request({"$ref": "input.prompt"}),
    }


def api_artifact_manifest(**overrides: Any) -> dict[str, Any]:
    base = manifest(
        id="api-artifact",
        name="API Artifact",
        description="Renders each reply through an external API and attaches the result.",
        requires={"operations": ["http.request", "artifact.emit", "ui.status"], "components": []},
        permissions=[
            {"capability": "context.read", "field": "draft"},
            {"capability": "network.request", "origin": API_ARTIFACT_ORIGIN},
            {"capability": "artifact.write"},
        ],
        secrets=[{"name": "api_key", "label": "API key", "description": "Sent as a bearer token to the render endpoint."}],
        hooks={"post_pipeline": {"flow": "flows/render.json", "stage": "observe"}},
        produces_artifacts=True,
        artifact_flows={
            "regenerate": "flows/recover.json",
            "reroll_gen": "flows/recover.json",
            "recovery_input_schema": FETCH_RECOVERY_SCHEMA,
        },
    )
    base.update(overrides)
    return base


def api_artifact_package(**overrides: Any) -> bytes:
    return orbext(
        {
            "orb-extension.json": api_artifact_manifest(**overrides),
            "flows/render.json": api_artifact_hook_flow(),
            "flows/recover.json": api_artifact_recovery_flow(),
        }
    )


# ── Outcome Resolver: the API 2 Writer-tool reference package ────────────────
# The reason `writer.tool.contribute`, `OpContext.WRITER_TOOL`, and the bounded
# Writer ReAct loop exist. It is the only package that answers the Writer
# *mid-reply*: the model pauses at an uncertain action, the resolver rolls
# against a difficulty and records the outcome on the conversation, and the
# Writer continues from exactly where it stopped.
#
# It deliberately exercises the whole profile rather than the minimum: host-
# supplied `ctx.draft` it never asked the model to echo, deterministic
# `random.integer` seeded from turn identity, namespaced conversation state
# whose commit boundary is the *flow's* success rather than the turn's, and a
# structured output schema the host validates before the Writer sees it.

OUTCOME_RESOLVER_ID = "outcome-resolver"

RESOLVE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "maxLength": 1000,
            "description": "What the character is attempting, in one sentence.",
        },
        "difficulty": {
            "type": "integer",
            "minimum": 1,
            "maximum": 20,
            "description": "How hard it is, from 1 (trivial) to 20 (near-impossible).",
        },
    },
    "required": ["action", "difficulty"],
    "additionalProperties": False,
}

RESOLVE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": ["success", "failure"]},
        "roll": {"type": "integer", "minimum": 1, "maximum": 20},
    },
    "required": ["outcome", "roll"],
    "additionalProperties": False,
}


def resolve_outcome_flow() -> dict[str, Any]:
    """Roll, classify, record what the Writer had written, return the verdict.

    The ``ctx.draft`` read is the point of the fixture, not decoration: the host
    supplies the exact prose streamed before the call, and recording it in
    conversation state is what lets a test assert that the model neither chose
    nor echoed it.

    The success/failure branch uses the idiom this language actually has. A
    guarded step produces a value only when its predicate holds, and a following
    step reads it with an explicit ``fallback`` -- there is no phi node, and a
    step id may be defined once, so "one of two values" is expressed as "this
    value, or that one if it never ran".

    The state write is also what makes the transaction boundary observable: it
    commits when *this flow* returns, which may be before the Writer's
    continuation succeeds or the user aborts the turn.
    """
    return {
        "flow_version": 1,
        "steps": [
            {"id": "roll", "op": "random.integer", "minimum": 1, "maximum": 20},
            {
                "id": "won",
                "op": "text.concat",
                "values": ["success"],
                "when": {"gte": [{"$ref": "steps.roll"}, {"$ref": "input.difficulty"}]},
            },
            {
                "id": "verdict",
                "op": "text.concat",
                "values": [{"$ref": "steps.won"}],
                "on_error": "continue",
                "fallback": "failure",
            },
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "last_roll",
                "value": {"$template": "{{input.action}} -> {{steps.verdict}} ({{steps.roll}})"},
            },
            {
                "op": "state.set",
                "scope": "conversation",
                "path": "draft_at_call",
                "value": {"$ref": "ctx.draft"},
            },
            {
                "op": "return",
                "value": {"outcome": {"$ref": "steps.verdict"}, "roll": {"$ref": "steps.roll"}},
            },
        ],
    }


def outcome_resolver_manifest(**overrides: Any) -> dict[str, Any]:
    base = manifest(
        extension_api=2,
        id=OUTCOME_RESOLVER_ID,
        name="Outcome Resolver",
        description="Resolves uncertain actions so the Writer does not decide them alone.",
        requires={
            "operations": ["random.integer", "text.concat", "state.set", "return"],
            "components": [],
        },
        permissions=[
            {"capability": "writer.tool.contribute"},
            {"capability": "context.read", "field": "draft"},
            {"capability": "state.write", "scope": "conversation"},
        ],
        contributions={
            "writer_tool": {
                "id": "resolve_outcome",
                "label": "Resolve outcome",
                "description": (
                    "Resolve an uncertain action when success or failure should not be chosen by the Writer alone."
                ),
                "flow": "flows/resolve-outcome.json",
                "input_schema": RESOLVE_INPUT_SCHEMA,
                "output_schema": RESOLVE_OUTPUT_SCHEMA,
            }
        },
    )
    base.update(overrides)
    return base


def outcome_resolver_package(**overrides: Any) -> bytes:
    return orbext(
        {
            "orb-extension.json": outcome_resolver_manifest(**overrides),
            "flows/resolve-outcome.json": resolve_outcome_flow(),
        }
    )


# ── API 3: a contributed audit detector ─────────────────────────────────────
# The Editor's mirror of the Writer tool. The flow reads ``ctx.draft`` and
# ``ctx.direction``, scores the draft with an isolated model call, and returns
# the host-fixed ``[{snippet, note}]`` shape -- the package declares no output
# schema, so it cannot widen what a finding is.

SLOP_SCORER_ID = "slop-scorer"


def score_slop_flow() -> dict[str, Any]:
    """Ask a model for the worst span in the draft, return it as one finding.

    ``ctx.direction`` is read for its own sake: it is what a scorer needs to
    judge a draft against the scene it was supposed to write, and it is the
    projection Phase 1 added. The ``model.structured`` call is what makes this a
    *classifier* detector rather than another static algorithm.
    """
    return {
        "flow_version": 1,
        "steps": [
            {
                "id": "score",
                "op": "model.structured",
                "lane": "agent",
                "prompt": {
                    "$template": (
                        "Scene direction: {{ctx.direction.scene_direction}}\n\n"
                        "Quote the single worst sentence of this reply, and say why in one line.\n\n"
                        "{{ctx.draft}}"
                    )
                },
                "output_schema": {
                    "type": "object",
                    "properties": {"snippet": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["snippet", "note"],
                    "additionalProperties": False,
                },
            },
            {
                "op": "return",
                "value": [
                    {
                        "snippet": {"$ref": "steps.score.snippet"},
                        "note": {"$ref": "steps.score.note"},
                    }
                ],
            },
        ],
    }


def audit_detector_manifest(**overrides: Any) -> dict[str, Any]:
    base = manifest(
        extension_api=3,
        id=SLOP_SCORER_ID,
        name="Slop Scorer",
        description="Scores each draft with a classifier instead of a static algorithm.",
        requires={"operations": ["model.structured", "return"], "components": []},
        permissions=[
            {"capability": "audit.detector.contribute"},
            {"capability": "context.read", "field": "draft"},
            {"capability": "context.read", "field": "direction"},
            {"capability": "model.call", "lane": "agent"},
        ],
        contributions={
            "audit_detectors": [
                {
                    "id": "slop",
                    "label": "Model-scored slop",
                    "description": "Flags the weakest sentence in each reply.",
                    "flow": "flows/score-slop.json",
                }
            ]
        },
    )
    base.update(overrides)
    return base


def audit_detector_package(**overrides: Any) -> bytes:
    return orbext(
        {
            "orb-extension.json": audit_detector_manifest(**overrides),
            "flows/score-slop.json": score_slop_flow(),
        }
    )
