# Secondary Workflows

A workflow is an optional feature that plugs into Orb without adding feature
logic to the core turn pipeline. It is a Python record in a process-local
registry, plus any hooks, state, attachments, and frontend code it needs.

Built-in examples are `tts`, `image_gen`, and `format_consistency`.

## What a workflow can do

A workflow may:

- add work before or after a generated turn;
- expose a conversation-scoped action or a conversation-less query;
- produce an attachment for a message and later regenerate, reroll, rehydrate,
  activate, or delete it;
- keep state at conversation, message, character, or global config scope;
- register frontend cards, buttons, widgets, SSE handlers, audio, text effects,
  or click actions.

The framework owns registration, routing, persistence, locks, and common UI
chrome. The workflow owns its feature logic.

## Where things live

### Backend

| Path | Purpose |
|---|---|
| `backend/workflows/registry.py` | Workflow records, subscriptions, lookups, and state access |
| `backend/workflows/contracts.py` | Hook types, context dataclasses, and `ToolSpec` |
| `backend/workflows/toolkit.py` | Stable imports for workflow authors |
| `backend/workflows/attachment_cache.py` | Attachment storage, variants, budget, and eviction |
| `backend/workflows/__init__.py` | Built-in registration and hook subscriptions |
| `backend/pipeline/workflow_bridge.py` | Pipeline hook dispatch and attachment staging |
| `backend/api/routes/workflows.py` | Workflow and attachment routes |

Each workflow has a directory such as `backend/workflows/tts/`.

### Frontend

| Path | Purpose |
|---|---|
| `frontend/workflow_api.js` | Public plugin facade |
| `frontend/workflow_loader.js` | Loads one module per manifest entry |
| `frontend/state.js` | Workflow registries and UI state |
| `frontend/chat.js` | SSE dispatch, widgets, cards, and refetching |
| `frontend/workflows/<id>/index.js` | Workflow entry point |
| `frontend/default_widget.js` | Fallback image, audio, video, or download view |

Frontend workflow code imports `/static/workflow_api.js` and its own relative
modules. It should not import core frontend modules directly.

## Declare and register a workflow

The workflow module declares data. The package-level `backend/workflows/__init__.py`
registers it and binds its hooks.

```python
Workflow(
    id="my_workflow",
    display_name="My workflow",
    tools=[],
    config_defaults={},
    config_schema=None,
    produces_artifacts=False,
)
```

`id` is the boundary key used in URLs, JSON, tools, and static module paths.
Tool names must be unique and must agree across `ToolSpec.name`, the schema,
and `tool_choice`.

Registration follows this shape:

```python
register_workflow(my_workflow)
subscribe(my_workflow.id, HookType.POST_PIPELINE, post_pipeline)
finalize_registry()
```

`finalize_registry()` verifies that an artifact-producing workflow has both
regeneration hooks. Registration order determines manifest order and is stable
on re-registration.

### Hook types

| Hook | Runs | Return shape |
|---|---|---|
| `PRE_PIPELINE` | During a turn, before the main passes; all hooks in priority order | Async stream of events or pipeline instructions |
| `POST_PIPELINE` | During a turn, after the main passes; all hooks in priority order | Async stream of events, draft changes, state, or attachments |
| `ON_DEMAND` | Conversation-scoped trigger route | One response object |
| `REGENERATE` | Attachment regeneration route | A list of new attachment records |
| `REROLL_GEN` | Attachment reroll and rehydrate routes | Bytes, or bytes plus consumption metadata |
| `QUERY` | Global configuration/discovery route | One response object |

`QUERY` has no conversation or LLM client. It is for setup and discovery, such
as checking an external server before a conversation exists. The message-level
regenerate route reruns the normal turn pipeline; `REGENERATE` is only for an
attachment.

## Enablement

The settings row is the source of truth:

| Setting | Meaning |
|---|---|
| `workflows_globally_enabled` | Master switch |
| `workflow_enabled` | JSON map of per-workflow overrides; missing means enabled |

Effective state is `global_on AND local_on`. The backend applies it to pipeline
hooks and hook-firing routes. Config, manifest, query, and attachment-consumption
routes remain available so a disabled workflow can be configured and existing
attachments can still be viewed.

The frontend mirrors the same predicate and hides disabled workflow controls.
Existing attachment renderers remain available by design.

## Hook contexts

Contexts are frozen dataclasses. Their mutable fields are read-only views, with
two deliberate exceptions: `turn_scratch` and the service objects used by the
framework.

| Context | Provides | Notes |
|---|---|---|
| `PreCtx` | Conversation, history, current user text, settings, prefix, tool map, client, cache tracker | `turn_scratch` is shared with PostCtx |
| `PostCtx` | Conversation, final history, effective user text, Director output, merged tools, prefix, client, cache tracker | May stage a draft, state, or attachment |
| `OnDemandCtx` | Conversation, history, current user text, settings, client, character | Trigger actions |
| `RegenCtx` | Conversation, message and attachment ids, pre-anchor history, settings, client, character | Attachment regeneration |
| `RerollGenCtx` | Conversation, message and attachment ids, settings, client, prior consumption metadata, `replay` | Shared by reroll and rehydrate |
| `QueryCtx` | Settings | No conversation and no client |

For group work, `character` identifies the relevant speaker. A
`RerollGenCtx` with `replay=True` reproduces stored generation parameters;
`replay=False` lets a new variant use current workflow settings.

## State and locks

State is JSON-backed and accessed through the toolkit. Use a matching lock for
read-modify-write operations.

| State | Scope | Lock |
|---|---|---|
| `workflow_state` | Conversation + workflow | `workflow_state_lock(cid, wid)` |
| `workflow_message_state` | Message + workflow | Owning conversation lock |
| `workflow_character_state` | Character + workflow | Conversation lock, then `workflow_character_state_lock` |
| `workflow_config` | Workflow | `workflow_config_lock()` |
| Attachments | Root attachment group | Framework's root lock |

The required import surface is `backend.workflows.toolkit`. It provides the LLM
client and prompt helpers, read-only database queries, state getters/setters,
`forced_tool_call`, attachment insertion, and the workflow locks. Mutating core
database helpers are intentionally not exposed to workflows.

## A workflow inside a turn

The bridge gives every turn one scratch dictionary, client, cache tracker, and
schema override map. It then follows this flow:

```text
PRE_PIPELINE hooks
        ↓
Director → Writer → Editor
        ↓
POST_PIPELINE hooks
        ↓
persist assistant message, state, and attachments
        ↓
SSE done
```

Pre-hooks can add system blocks, enable tools, or emit public events. Post-hooks
can replace the draft, set message state, stage attachments, or emit public
events. Hooks run in subscription priority order. A hook failure is isolated so
the main reply and other workflows can continue.

Use `forced_tool_call` for a one-shot tool call. Pass the context's prefix,
enabled tools, schema overrides, client, and cache tracker so the call follows
the same prompt and cache rules as the main turn. Its `token_floor` is what the
call needs to answer in full; the Agent lane's configured `max_tokens` raises it
when that endpoint has more room, and never lowers it below the floor.

Public hook events pass through to SSE. Core events and names beginning with
`_` are reserved. A useful custom event is `phase_status` with a channel that
starts with `workflow:<id>`.

## Attachments

Artifact-producing workflows write through `insert_workflow_attachment` or
yield an `attach_artifact` instruction from `POST_PIPELINE`. An attachment has
a workflow id, filename, MIME type, and exactly one byte source (`data` or
`path`). The framework validates it before persistence.

Attachments are arranged as a flat variant group: one root and its siblings.
The active sibling is user-selectable. The cache stores bytes in
`workflow_attachments` and enforces a configurable byte budget. When space is
needed, older accessed rows are evicted by replacing their bytes with the
`[evicted]` marker.

Supply a seed and JSON generation metadata when an artifact can be recreated.
That lets the user rehydrate evicted bytes. The same `REROLL_GEN` hook handles:

- **reroll** — a new seed and a new sibling using current settings;
- **rehydrate** — the stored seed and parameters, restoring the same row.

`REGENERATE` also creates siblings, while `activate` and `delete` only change
the variant group. Existing artifacts remain readable when their workflow is
disabled.

## HTTP surface

The framework exposes:

```text
GET  /api/workflows
GET  /api/workflows/{wid}/config
PUT  /api/workflows/{wid}/config
POST /api/workflows/{wid}/enabled
POST /api/workflows/{wid}/query
POST /api/conversations/{cid}/workflows/{wid}/trigger
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate
POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete
POST /api/conversations/{cid}/workflow-attachments/access
```

The manifest returns workflow identity and config form metadata. Config is a
full replacement; a workflow's `config_normalizer` owns its valid shape and is
used on both read and write.

## Frontend integration

At boot, the frontend fetches the manifest and imports
`/static/workflows/<id>/index.js` for each entry. Top-level registration calls
run when the module loads.

The facade in `workflow_api.js` is the frontend ABI. It is additive-only: new
exports may be added, but existing names and signatures do not change. Common
registration points are:

```js
registerWorkflowInspectorCard(wid, render)
registerWorkflowToolsPanelCard(wid, render)
registerWorkflowMessageButton(wid, render)
registerWorkflowEventHandler(wid, event, handler)
registerAttachmentRenderer(wid, render)
registerWorkflowPipeline({ id: wid, passes })
registerTextEffect({ id, label })
registerClickHandler({ id, claims, onClick })
```

Use `registerAttachmentRenderer` for the workflow's own widgets. It is not
enablement-gated so stored artifacts remain visible. Other workflow-owned cards,
buttons, and event handlers are gated by workflow id.

Buttons and inputs use delegated actions rather than globals or inline event
handlers:

```html
<button data-wf-action="my_workflow:refresh">Refresh</button>
```

```js
registerAction("my_workflow", "refresh", (element, event) => { /* ... */ });
```

The facade also provides API helpers, modal and notification helpers, workflow
phases, shared audio controls, text effects, message access, group cast data,
and conversation repaint/refetch helpers.

## Authoring checklist

1. Create `backend/workflows/<id>/` and declare a `Workflow` record.
2. Implement hooks with the context and return shapes above.
3. Register the workflow and subscriptions in
   `backend/workflows/__init__.py`.
4. Use the toolkit and matching locks for state changes.
5. If producing artifacts, implement `REGENERATE` and `REROLL_GEN`, and store
   recovery metadata where rehydrate is useful.
6. Create `frontend/workflows/<id>/index.js` and import only the facade plus
   relative modules.
7. Register only non-reserved SSE events and use `data-wf-action` for controls.
8. Add config defaults/schema and normalize the effective config if needed.

## Quick lookup

| Need | Start here |
|---|---|
| Add a hook | `contracts.py`, `registry.py`, and `workflow_bridge.py` |
| Call a tool | `toolkit.forced_tool_call` |
| Store workflow state | Toolkit state helpers and the matching lock |
| Produce an attachment | `attach_artifact` and `attachment_cache.py` |
| Add a custom stream event | Hook event plus `registerWorkflowEventHandler` |
| Add UI | `workflow_api.js` registrars and `registerAction` |
